"""Tests for the fleet-audit procedural collector (collect.py).

Golden-dump tests for every check the three streams convert — filters that
finally get the tests prose alone could never have — plus fault
injection at every seam the manifest exists to make honest: a zero-byte
dump, a truncated one, one cluster's get-credentials failing under
parallelism, and both never reading as a shorter candidate list.
"""

import hashlib
import inspect
import io
import json
import re
import shlex
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import collect  # noqa: E402
from collect import Run  # noqa: E402


def deployment(name, ns="default", **overrides):
    doc = {
        "kind": "Deployment",
        "metadata": {"namespace": ns, "name": name, "labels": {}, "annotations": {}},
        "spec": {
            "replicas": 2,
            "template": {"spec": {"containers": [{"name": "app", "resources": {}}]}},
        },
    }
    for path, value in overrides.items():
        target = doc
        keys = path.split(".")
        for key in keys[:-1]:
            target = target.setdefault(key, {})
        target[keys[-1]] = value
    return doc


def dump_of(*items):
    return {"items": list(items)}


def with_container_resources(dep, resources):
    dep["spec"]["template"]["spec"]["containers"][0]["resources"] = resources
    return dep


class TestNormalizeWorkloads(unittest.TestCase):
    def test_a_plain_deployment_survives(self):
        out = collect.normalize_workloads(dump_of(deployment("api")))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "api")

    def test_system_namespace_is_excluded(self):
        out = collect.normalize_workloads(dump_of(deployment("coredns", ns="kube-system")))
        self.assertEqual(out, [])

    def test_a_gke_prefixed_namespace_is_excluded(self):
        out = collect.normalize_workloads(dump_of(deployment("x", ns="gke-connect")))
        self.assertEqual(out, [])

    def test_gke_managed_addon_is_excluded(self):
        d = deployment("fluentbit")
        d["metadata"]["labels"]["addonmanager.kubernetes.io/mode"] = "Reconcile"
        self.assertEqual(collect.normalize_workloads(dump_of(d)), [])

    def test_a_workload_with_an_owner_is_excluded(self):
        d = deployment("replicaset-child")
        d["metadata"]["ownerReferences"] = [{"kind": "ReplicaSet", "name": "x"}]
        self.assertEqual(collect.normalize_workloads(dump_of(d)), [])

    def test_a_workload_owned_by_a_job_is_excluded(self):
        d = deployment("batch-child")
        d["metadata"]["ownerReferences"] = [{"apiVersion": "batch/v1", "kind": "Job", "name": "x"}]
        self.assertEqual(collect.normalize_workloads(dump_of(d)), [])

    def test_a_workload_owned_by_a_crd_is_still_audited(self):
        """S3 defers to the owning controller. A CRD is not a controller this
        audit ever reads, so deferring to it drops the finding instead of
        moving it — which is how the harness's own gateway went permanently
        unaudited in the one namespace S1 keeps in scope on purpose."""
        d = deployment("platform-agent-gateway", ns="kubeagents-system")
        d["metadata"]["ownerReferences"] = [
            {"apiVersion": "kubeagents.x-k8s.io/v1alpha1", "kind": "PlatformAgent", "name": "platform-agent"}
        ]
        out = collect.normalize_workloads(dump_of(d))
        self.assertEqual([w["name"] for w in out], ["platform-agent-gateway"])

    def test_a_crd_that_borrows_a_builtin_kind_name_does_not_suppress(self):
        """`Job` in someone else's API group is a custom resource wearing the
        name, and nothing about it is reachable from this dump."""
        d = deployment("look-alike")
        d["metadata"]["ownerReferences"] = [{"apiVersion": "acme.example.com/v1", "kind": "Job", "name": "x"}]
        self.assertEqual([w["name"] for w in collect.normalize_workloads(dump_of(d))], ["look-alike"])

    def test_one_builtin_owner_is_enough_to_suppress(self):
        d = deployment("two-owners")
        d["metadata"]["ownerReferences"] = [
            {"apiVersion": "acme.example.com/v1", "kind": "Widget", "name": "w"},
            {"apiVersion": "apps/v1", "kind": "Deployment", "name": "d"},
        ]
        self.assertEqual(collect.normalize_workloads(dump_of(d)), [])

    def test_the_opt_out_label_is_excluded(self):
        d = deployment("exempted")
        d["metadata"]["labels"]["kubeagents.x-k8s.io/reliability-audit"] = "exempt"
        self.assertEqual(collect.normalize_workloads(dump_of(d)), [])

    def test_the_opt_out_annotation_is_also_honored(self):
        d = deployment("exempted")
        d["metadata"]["annotations"]["kubeagents.x-k8s.io/reliability-audit"] = "exempt"
        self.assertEqual(collect.normalize_workloads(dump_of(d)), [])

    def test_a_scaled_to_zero_workload_is_excluded(self):
        d = deployment("idle", **{"spec.replicas": 0})
        self.assertEqual(collect.normalize_workloads(dump_of(d)), [])

    def test_non_workload_kinds_are_ignored(self):
        pdb = {"kind": "PodDisruptionBudget", "metadata": {"namespace": "default", "name": "x"}}
        self.assertEqual(collect.normalize_workloads(dump_of(pdb)), [])

    def test_the_reconciler_travels_with_the_workload(self):
        d = deployment("litellm", ns="kubeagents-system")
        d["metadata"]["annotations"]["meta.helm.sh/release-name"] = "kube-agents"
        out = collect.normalize_workloads(dump_of(d))
        self.assertEqual(out[0]["reconciler"], "the Helm release `kube-agents`")

    def test_an_unreconciled_workload_carries_none(self):
        out = collect.normalize_workloads(dump_of(deployment("api")))
        self.assertIsNone(out[0]["reconciler"])


class TestReconcilerOf(unittest.TestCase):
    """What continuously reasserts an object's spec, read off the object.

    All three markers are written at apply time and sit in the dump every check
    already runs on, so naming the reconciler costs no extra call. The phrase
    ends up in the remediation note of any `manual` finding on the object --
    `audit_report.disclose_reconciled_manual_remediations` -- which is why each
    one names the release or Application rather than saying "something".
    """

    def meta(self, annotations=None, labels=None):
        return {"annotations": annotations or {}, "labels": labels or {}}

    def test_a_helm_release_is_named_with_its_namespace(self):
        # The release namespace is not the object's: cert-manager's Deployments
        # live in `cert-manager` under a release of the same name, while the
        # kube-agents chart installs into `kubeagents-system`.
        self.assertEqual(
            collect.reconciler_of(
                self.meta(
                    {
                        "meta.helm.sh/release-name": "kube-agents",
                        "meta.helm.sh/release-namespace": "kubeagents-system",
                    }
                )
            ),
            "the Helm release `kube-agents` in kubeagents-system",
        )

    def test_a_helm_release_without_a_namespace_is_still_named(self):
        self.assertEqual(
            collect.reconciler_of(self.meta({"meta.helm.sh/release-name": "cert-manager"})),
            "the Helm release `cert-manager`",
        )

    def test_an_argocd_application_is_the_leading_segment(self):
        # `<application>:<group>/<Kind>:<namespace>/<name>`.
        self.assertEqual(
            collect.reconciler_of(
                self.meta(
                    {
                        "argocd.argoproj.io/tracking-id": (
                            "waste-canary:apps/Deployment:spot-capacity-test/waste-unsized"
                        )
                    }
                )
            ),
            "the Argo CD Application `waste-canary`",
        )

    def test_helm_outranks_argocd(self):
        # Argo CD tracks objects it installs from a chart too, and the release
        # is the closer of the two: a `helm upgrade` reverts a hand patch
        # whether or not an Application is also syncing it.
        self.assertEqual(
            collect.reconciler_of(
                self.meta(
                    {
                        "meta.helm.sh/release-name": "kube-agents",
                        "argocd.argoproj.io/tracking-id": "apps:apps/Deployment:ns/x",
                    }
                )
            ),
            "the Helm release `kube-agents`",
        )

    def test_managed_by_is_the_fallback(self):
        self.assertEqual(
            collect.reconciler_of(
                self.meta(labels={"app.kubernetes.io/managed-by": "kube-agents-operator"})
            ),
            "`kube-agents-operator`",
        )

    def test_the_helm_literal_is_not_a_reconciler(self):
        # Helm sets `managed-by: Helm`, which names no release. Reporting it
        # would send a reader looking for a release called "Helm".
        self.assertIsNone(
            collect.reconciler_of(self.meta(labels={"app.kubernetes.io/managed-by": "Helm"}))
        )

    def test_an_unmanaged_object_has_no_reconciler(self):
        self.assertIsNone(collect.reconciler_of(self.meta()))

    def test_blank_marker_values_do_not_count(self):
        self.assertIsNone(
            collect.reconciler_of(
                self.meta(
                    {"meta.helm.sh/release-name": "  ", "argocd.argoproj.io/tracking-id": ""},
                    {"app.kubernetes.io/managed-by": ""},
                )
            )
        )

    def test_absent_annotation_and_label_maps_are_tolerated(self):
        self.assertIsNone(collect.reconciler_of({}))
        self.assertIsNone(collect.reconciler_of({"annotations": None, "labels": None}))


ARGO_TRACKING = {"argocd.argoproj.io/tracking-id": "workloads-gitops:apps/Deployment:ai/x"}
ARGO_PHRASE = "the Argo CD Application `workloads-gitops`"
HELM_MARKERS = {"meta.helm.sh/release-name": "platform", "meta.helm.sh/release-namespace": "infra"}
HELM_PHRASE = "the Helm release `platform` in infra"


def marked(doc, annotations):
    """`doc` with reconciler markers on it, for a check that must read them."""
    doc["metadata"].setdefault("annotations", {}).update(annotations)
    return doc


class TestClusterCheckReconcilers(unittest.TestCase):
    """A cluster-scoped check names the reconciler of the object it reports.

    "Cluster-scoped" means the check runs once per cluster rather than once
    per workload. It does not mean the finding is about the cluster: six of
    the ten name an ordinary namespaced object, and until 2026-09-06 `emit`
    set `reconciler` only when a workload record supplied it, so all six lost
    the field. What that cost, live: `inference-endpoint-public` on
    `Service/ai-inference-unsafe`, always `manual` per §3.5, telling the owner
    to choose a caller range by hand -- on a Service the Argo CD Application
    `workloads-adamparco-gitops` reasserts on every sync.

    Each test asserts the reconciler of the object in `hit["object"]`, not of
    whatever else the check had in hand. Two of them had both.
    """

    def test_a_public_inference_endpoint_names_the_services_reconciler(self):
        svc = marked(ai_service("vllm-svc", selector={"app": "vllm"}, ingress=["136.70.153.197"]), ARGO_TRACKING)
        ctx = {"services": [svc], "ai_workloads": [{"ns": "default", "lbl": {"app": "vllm"}}]}
        hits = collect.check_inference_endpoint_public(ctx)
        self.assertEqual(hits[0]["object"], "Service/vllm-svc")
        self.assertEqual(hits[0]["reconciler"], ARGO_PHRASE)

    def test_a_blocking_pdb_names_its_own_reconciler_not_the_workloads(self):
        """The discrimination that makes this worth a test rather than a
        one-liner. The check has two objects in hand and reports one of them;
        naming the Deployment's chart would send the reader to a file that
        does not contain the field they have to change."""
        d = deployment("api", **{"spec.replicas": 3})
        d["spec"]["template"]["metadata"] = {"labels": {"app": "api"}}
        marked(d, ARGO_TRACKING)
        ctx = context_of(
            pdbs={"default": [marked(pdb("p", max_unavailable=0), HELM_MARKERS)]},
            workloads=collect.normalize_workloads(dump_of(d)),
        )
        hits = collect.check_blocking_pdb(ctx)
        self.assertEqual(hits[0]["object"], "PodDisruptionBudget/p")
        self.assertEqual(hits[0]["reconciler"], HELM_PHRASE)

    def test_a_pinned_hpa_names_its_reconciler(self):
        ctx = context_of(hpas={"default": [marked(hpa("h", min_replicas=3, max_replicas=3), ARGO_TRACKING)]})
        hits = collect.check_hpa_cannot_scale(ctx)
        self.assertEqual(hits[0]["severity"], "major")
        self.assertEqual(hits[0]["reconciler"], ARGO_PHRASE)

    def test_a_dangling_hpa_names_its_reconciler(self):
        """The second arm, separately. They append separate dicts, so the
        field can be present on one and missing on the other."""
        dangling = hpa("h", target={"apiVersion": "apps/v1", "kind": "Deployment", "name": "gone"})
        ctx = context_of(
            hpas={"default": [marked(dangling, HELM_MARKERS)]},
            workloads=collect.normalize_workloads(dump_of(deployment("other"))),
        )
        hits = collect.check_hpa_cannot_scale(ctx)
        self.assertEqual(hits[0]["severity"], "minor")
        self.assertEqual(hits[0]["reconciler"], HELM_PHRASE)

    def test_a_cluster_admin_binding_names_its_reconciler(self):
        binding = marked(crb("admin-binding", [subject("ServiceAccount", "app", "default")]), ARGO_TRACKING)
        hits = collect.check_cluster_admin_binding(context_of(clusterrolebindings=[binding]))
        self.assertEqual(hits[0]["object"], "ClusterRoleBinding/admin-binding")
        self.assertEqual(hits[0]["reconciler"], ARGO_PHRASE)

    def test_a_wildcard_role_names_the_roles_reconciler_not_the_bindings(self):
        """The other check holding two objects. The excerpt names the bound
        principals, which come from the binding; the remediation narrows the
        rule, which lives on the role."""
        role = marked(
            cluster_role("too-broad", [{"apiGroups": ["*"], "resources": ["*"], "verbs": ["*"]}]),
            HELM_MARKERS,
        )
        binding = marked(
            role_binding("ClusterRole", "too-broad", [subject("ServiceAccount", "app", "default")]),
            ARGO_TRACKING,
        )
        ctx = context_of(roles=[role], clusterrolebindings=[binding], rolebindings=[])
        hits = collect.check_wildcard_rbac(ctx)
        self.assertEqual(hits[0]["object"], "ClusterRole/too-broad")
        self.assertEqual(hits[0]["reconciler"], HELM_PHRASE)

    def test_default_sa_automount_names_the_workloads_reconciler(self):
        """A cluster check that nonetheless reports a workload: it walks
        `context["workloads"]` itself because the ServiceAccount half of the
        test is a namespace fact, so the per-workload plumbing never reaches
        it."""
        d = marked(deployment("api"), ARGO_TRACKING)
        ctx = context_of(
            workloads=collect.normalize_workloads(dump_of(d)),
            serviceaccounts=[{"metadata": {"namespace": "default", "name": "default"}}],
        )
        hits = collect.check_default_sa_automount(ctx)
        self.assertEqual(hits[0]["object"], "Deployment/api")
        self.assertEqual(hits[0]["reconciler"], ARGO_PHRASE)

    def test_netpol_missing_names_no_reconciler_on_purpose(self):
        """The deliberate exclusion. The object is a Namespace and the fix is
        a NetworkPolicy that does not exist yet, so "a change applied by hand
        is reverted" would be false -- whatever holds the Namespace does not
        delete a policy created beside it."""
        ns = marked(namespace("payments"), ARGO_TRACKING)
        ctx = context_of(
            namespaces=[ns],
            networkpolicies=[],
            workloads=[{"kind": "Pod", "ns": "payments", "name": "api"}],
        )
        hits = collect.check_netpol_missing(ctx)
        self.assertEqual(hits[0]["object"], "Namespace/payments")
        self.assertIsNone(hits[0].get("reconciler"))

    def test_an_unmarked_object_leaves_the_field_unset(self):
        ctx = context_of(hpas={"default": [hpa("h", min_replicas=3, max_replicas=3)]})
        self.assertIsNone(collect.check_hpa_cannot_scale(ctx)[0]["reconciler"])


def context_of(dump=None, **overrides):
    """A build_context()-shaped dict with sensible empty defaults, so a test
    that only cares about one cross-reference does not have to construct the
    other three."""
    base = {
        "claims": {},
        "limitranges": {},
        "pdbs": {},
        "hpas": {},
        "services": {},
        "cronjobs": [],
        "service_endpoints": [],
        "workloads": [],
        "workload_keys": set(),
        "pod_namespaces": set(),
        "cluster_name": "test-cluster",
    }
    if dump is not None:
        base.update(
            claims=collect.claims_by_key(dump),
            limitranges=collect.limitranges_by_namespace(dump),
            pdbs=collect.pdbs_by_namespace(dump),
            hpas=collect.hpas_by_namespace(dump),
            services=collect.services_by_namespace(dump),
            cronjobs=collect.cronjobs_with_jobs(dump),
            service_endpoints=collect.services_with_endpoints(dump),
            workloads=collect.normalize_workloads(dump),
            workload_keys=collect.workload_keys(dump),
        )
    base.update(overrides)
    if "workload_keys" not in overrides and dump is None:
        # A test that hands over `workloads` and no dump is saying "this is
        # what the cluster holds", so the two have to agree. Deriving keeps
        # those tests about the check they name; the S4/S5 cases that need the
        # two sets to *differ* pass a dump, or `workload_keys` outright.
        base["workload_keys"] = {(wl["ns"], wl["kind"], wl["name"]) for wl in base["workloads"]}
    if "pod_namespaces" not in overrides:
        # Same rule for the raw pod set `netpol-missing` reads. A test that
        # says the namespace holds a Pod means the cluster has one there; the
        # cases about the gap between the two -- an owned pod the audited set
        # drops, a namespace whose only Pod is in `pod_namespaces` and nowhere
        # else -- pass it outright.
        base["pod_namespaces"] = {wl["ns"] for wl in base["workloads"] if wl["kind"] == "Pod"}
    return base


class TestNoRequests(unittest.TestCase):
    def check(self, workload, limitranges=None):
        return collect.check_no_requests(workload, context_of(limitranges=limitranges or {}))

    def wl(self, resources=None, init_containers=None):
        d = deployment("api")
        if resources is not None:
            d["spec"]["template"]["spec"]["containers"][0]["resources"] = resources
        if init_containers is not None:
            d["spec"]["template"]["spec"]["initContainers"] = init_containers
        return collect.normalize_workloads(dump_of(d))[0]

    def test_no_requests_at_all_is_flagged(self):
        hit = self.check(self.wl(resources={}))
        self.assertIsNotNone(hit)
        self.assertEqual(hit["object"], "Deployment/api")
        self.assertIn("cpu", hit["excerpt"])
        self.assertIn("memory", hit["excerpt"])

    def test_both_requests_present_is_not_flagged(self):
        hit = self.check(self.wl(resources={"requests": {"cpu": "100m", "memory": "128Mi"}}))
        self.assertIsNone(hit)

    def test_missing_only_memory_is_flagged_by_name(self):
        hit = self.check(self.wl(resources={"requests": {"cpu": "100m"}}))
        self.assertIn("memory", hit["excerpt"])
        self.assertNotIn("cpu:", hit["excerpt"])

    def test_a_limitrange_default_request_suppresses_the_finding(self):
        limitranges = {
            "default": [{"spec": {"limits": [{"defaultRequest": {"cpu": "50m", "memory": "64Mi"}}]}}]
        }
        hit = self.check(self.wl(resources={}), limitranges)
        self.assertIsNone(hit)

    def test_a_limitrange_in_a_different_namespace_does_not_help(self):
        limitranges = {
            "other-ns": [{"spec": {"limits": [{"defaultRequest": {"cpu": "50m", "memory": "64Mi"}}]}}]
        }
        hit = self.check(self.wl(resources={}), limitranges)
        self.assertIsNotNone(hit)

    def test_a_native_sidecar_is_covered(self):
        # restartPolicy: Always makes an initContainer count toward the pod's
        # effective request set (§3.1) -- a plain init container never does.
        hit = self.check(
            self.wl(
                resources={"requests": {"cpu": "1", "memory": "1Gi"}},
                init_containers=[{"name": "proxy", "restartPolicy": "Always", "resources": {}}],
            )
        )
        self.assertIsNotNone(hit)
        self.assertIn("proxy", hit["excerpt"])

    # -- Impact, per arm. §3.1 flags a container missing cpu *or* memory, so
    # the check's own "first evicted under node pressure" describes only the
    # BestEffort arm. The other two carry their own sentence, composed of two
    # independently-varying halves: what the missing requests cost, and what
    # the pod's real QoS class means for eviction order.

    def test_a_besteffort_pod_keeps_the_checks_own_impact(self):
        # Nothing declared anywhere: the pod really is BestEffort and really is
        # evicted first, so this arm publishes the check's own sentence. It is
        # set on the hit rather than left to `CheckSpec.impact` so the arm is
        # flagged authoritative like the other two -- an arm that falls through
        # to the table is one `adopt_arm_impact` never reaches, and the model
        # is then free to publish a Burstable sentence over a BestEffort pod.
        hit = self.check(self.wl(resources={}))
        self.assertEqual(hit["impact"], collect._IMPACT_BEST_EFFORT)
        self.assertIn("first evicted", hit["impact"])
        self.assertNotIn("Burstable", hit["impact"])

    def test_a_burstable_pod_is_not_called_first_evicted(self):
        # A cpu request and no memory request. Ubiquitous as a shape --
        # `kube-proxy` and `antrea-controller` both ship it -- but never as a
        # finding from those two, whose namespace S1 drops; this arm is for a
        # user-namespace workload in the same shape.
        hit = self.check(self.wl(resources={"requests": {"cpu": "100m"}}))
        self.assertIn("Burstable, not BestEffort", hit["impact"])
        self.assertIn("memory goes unreserved", hit["impact"])
        self.assertNotIn("first evicted", hit["impact"])
        # And it must not swap one false eviction claim for another. The
        # kubelet does not rank by QoS class at all -- it sorts on whether
        # usage exceeds requests, then Pod Priority -- so "Burstable is evicted
        # after every BestEffort pod" is as wrong as "first evicted" was.
        self.assertIn("Eviction does not follow the class", hit["impact"])
        self.assertNotIn("after every BestEffort pod", hit["impact"])

    def test_a_limit_with_no_request_is_reserved_at_its_ceiling(self):
        # Kubernetes copies the limit into the request at admission, so this pod
        # is Guaranteed -- the last thing evicted. Still flagged, because §3.1
        # wants the request declared, but for the opposite reason.
        hit = self.check(self.wl(resources={"limits": {"cpu": "1", "memory": "1Gi"}}))
        self.assertIn("copies that limit into the request", hit["impact"])
        self.assertIn("Guaranteed", hit["impact"])
        self.assertIn("last group evicted", hit["impact"])
        self.assertNotIn("costs nothing", hit["impact"])

    # -- The eviction half is the *pod's* QoS class, which a sibling container
    # can decide. Reading it off the reported container's own limits is the
    # mistake these four pin: each has every missing request limit-backed, so
    # the ceiling sentence is right and "Guaranteed" is wrong.

    def test_a_sibling_without_a_limit_makes_a_backed_pod_burstable(self):
        # `app` declares limits and no requests -- backed, ceiling-reserved. But
        # `proxy` declares requests and no ceiling, and Guaranteed needs *every*
        # container to carry both limits. The pod is Burstable.
        hit = self.check(
            self.wl(
                resources={"limits": {"cpu": "1", "memory": "1Gi"}},
                init_containers=[
                    {
                        "name": "proxy",
                        "restartPolicy": "Always",
                        "resources": {"requests": {"cpu": "10m", "memory": "8Mi"}},
                    }
                ],
            )
        )
        self.assertIn("copies that limit into the request", hit["impact"])
        self.assertIn("Burstable, not BestEffort", hit["impact"])
        self.assertNotIn("Guaranteed", hit["impact"])

    def test_a_request_below_its_own_limit_is_not_guaranteed(self):
        # `memory` is missing and backed by its limit, so the ceiling sentence
        # holds -- but the declared cpu request is under the cpu limit, which is
        # the textbook Burstable pod.
        hit = self.check(
            self.wl(resources={"requests": {"cpu": "500m"}, "limits": {"cpu": "1", "memory": "1Gi"}})
        )
        self.assertIn("copies that limit into the request", hit["impact"])
        self.assertIn("Burstable, not BestEffort", hit["impact"])
        self.assertNotIn("Guaranteed", hit["impact"])

    def test_a_limitrange_default_below_the_limit_is_not_guaranteed(self):
        # The LimitRange covers cpu, so cpu drops out of `missing` and memory is
        # the only finding -- backed by its limit. But the injected cpu request
        # is 50m against a 1-core limit, so the admitted pod is Burstable.
        limitranges = {"default": [{"spec": {"limits": [{"defaultRequest": {"cpu": "50m"}}]}}]}
        hit = self.check(self.wl(resources={"limits": {"cpu": "1", "memory": "1Gi"}}), limitranges)
        self.assertIn("memory", hit["excerpt"])
        self.assertNotIn("cpu", hit["excerpt"])
        self.assertIn("Burstable, not BestEffort", hit["impact"])
        self.assertNotIn("Guaranteed", hit["impact"])

    def test_a_sibling_with_matching_requests_and_limits_stays_guaranteed(self):
        # The control for the three above: `proxy` declares both limits with
        # requests that match them, so nothing disqualifies the pod and the
        # Guaranteed sentence is the true one.
        hit = self.check(
            self.wl(
                resources={"limits": {"cpu": "1", "memory": "1Gi"}},
                init_containers=[
                    {
                        "name": "proxy",
                        "restartPolicy": "Always",
                        "resources": {
                            "requests": {"cpu": "10m", "memory": "8Mi"},
                            "limits": {"cpu": "10m", "memory": "8Mi"},
                        },
                    }
                ],
            )
        )
        self.assertIn("Guaranteed", hit["impact"])
        self.assertNotIn("Burstable", hit["impact"])

    # -- Which containers, and which quantities, Kubernetes counts. The QoS
    # computation reads a different container set from §3.1's flag-when and
    # ignores quantities §3.1 does not, so both had to be answered separately.

    def test_a_plain_init_containers_requests_decide_the_class(self):
        # `_effective_containers` drops a plain init container, correctly: it
        # never counts toward the pod's effective request. QoS is the other
        # question -- upstream iterates *all* of `spec.initContainers` with no
        # restartPolicy filter -- so this pod is Burstable, not BestEffort, and
        # must not fall through to the check's own "first evicted".
        hit = self.check(
            self.wl(
                resources={},
                init_containers=[{"name": "setup", "resources": {"requests": {"cpu": "100m"}}}],
            )
        )
        self.assertEqual(hit["excerpt"], "app: missing cpu,memory")
        self.assertIn("Burstable, not BestEffort", hit["impact"])

    def test_a_resourceless_plain_init_container_breaks_guaranteed(self):
        # Same container set, opposite direction: `app` alone would be
        # Guaranteed, but `migrate` carries no limits and every container needs
        # both for the class.
        hit = self.check(
            self.wl(
                resources={"limits": {"cpu": "1", "memory": "1Gi"}},
                init_containers=[{"name": "migrate", "resources": {}}],
            )
        )
        self.assertIn("copies that limit into the request", hit["impact"])
        self.assertIn("Burstable, not BestEffort", hit["impact"])
        self.assertNotIn("Guaranteed", hit["impact"])

    def test_an_extended_resource_alone_leaves_the_pod_besteffort(self):
        # Kubernetes counts cpu and memory and nothing else, so a container
        # asking only for a GPU is BestEffort -- the one arm where the check's
        # own "first evicted" is true. Calling it Burstable would state the
        # exact inverse.
        hit = self.check(self.wl(resources={"limits": {"nvidia.com/gpu": "1"}}))
        self.assertEqual(hit["impact"], collect._IMPACT_BEST_EFFORT)

    def test_an_explicit_zero_request_leaves_the_pod_besteffort(self):
        # A quantity has to be greater than zero to count.
        for resources in ({"requests": {"cpu": "0"}}, {"limits": {"memory": "0Mi"}}):
            with self.subTest(resources=resources):
                hit = self.check(self.wl(resources=resources))
                self.assertEqual(hit["impact"], collect._IMPACT_BEST_EFFORT)

    def test_a_zero_limit_does_not_make_a_pod_guaranteed(self):
        hit = self.check(self.wl(resources={"limits": {"cpu": "1", "memory": "0"}}))
        self.assertIn("Burstable, not BestEffort", hit["impact"])
        self.assertNotIn("Guaranteed", hit["impact"])

    def test_the_unreserved_claim_is_scoped_to_a_container_not_the_pod(self):
        # Two containers, each limiting a different resource. Both are missing
        # both requests, so the union is {cpu, memory} -- but the pod is sized
        # with cpu (from `app`) *and* memory (from `proxy`), both defaulted from
        # their limits. A pod-level "sized without cpu or memory" would be
        # flatly false; the container-scoped sentence is true.
        hit = self.check(
            self.wl(
                resources={"limits": {"cpu": "1"}},
                init_containers=[
                    {
                        "name": "proxy",
                        "restartPolicy": "Always",
                        "resources": {"limits": {"memory": "1Gi"}},
                    }
                ],
            )
        )
        self.assertIn("cpu or memory goes unreserved on at least one container", hit["impact"])
        self.assertNotIn("size this cluster without", hit["impact"])

    def test_two_spellings_of_one_quantity_fall_to_burstable(self):
        # `0.1` and `100m` are the same quantity and Kubernetes would call this
        # Guaranteed. `_qos_class` compares strings, so it says Burstable --
        # wrong, but in the direction that claims less. Pinned so a later change
        # to real quantity parsing is a deliberate one.
        self.assertEqual(
            collect._qos_class(
                [{"resources": {"requests": {"cpu": "0.1", "memory": "1Gi"}, "limits": {"cpu": "100m", "memory": "1Gi"}}}],
                {},
                "default",
            ),
            "Burstable",
        )

    def test_a_limit_covering_only_one_resource_leaves_the_other_unreserved(self):
        # A memory limit backs the memory request; CPU is backed by nothing, so
        # the sentence must name CPU and only CPU as unreserved.
        hit = self.check(self.wl(resources={"limits": {"memory": "1Gi"}}))
        self.assertIn("cpu goes unreserved", hit["impact"])
        self.assertNotIn("memory", hit["impact"].split("Burstable")[0])

    def test_the_unreserved_resources_are_named_in_sorted_order(self):
        # Both missing but a sibling container declares one, so the pod is
        # Burstable rather than BestEffort while nothing backs either resource.
        hit = self.check(
            self.wl(
                resources={},
                init_containers=[
                    {"name": "proxy", "restartPolicy": "Always", "resources": {"requests": {"cpu": "10m", "memory": "8Mi"}}}
                ],
            )
        )
        self.assertIn("cpu or memory goes unreserved", hit["impact"])

    def test_a_plain_init_container_is_never_flagged(self):
        hit = self.check(
            self.wl(
                resources={"requests": {"cpu": "1", "memory": "1Gi"}},
                init_containers=[{"name": "migrate", "resources": {}}],
            )
        )
        self.assertIsNone(hit)

    def test_a_wrong_sized_but_present_request_is_never_flagged(self):
        # This check owns absence only -- sizing is the waste audit's job.
        hit = self.check(self.wl(resources={"requests": {"cpu": "1m", "memory": "1Mi"}}))
        self.assertIsNone(hit)


class TestNoMemoryLimit(unittest.TestCase):
    def wl(self, resources=None):
        d = deployment("api")
        if resources is not None:
            d["spec"]["template"]["spec"]["containers"][0]["resources"] = resources
        return collect.normalize_workloads(dump_of(d))[0]

    def test_missing_memory_limit_is_flagged(self):
        hit = collect.check_no_memory_limit(self.wl(resources={}), context_of())
        self.assertIsNotNone(hit)
        self.assertIn("app", hit["excerpt"])

    def test_a_present_memory_limit_is_not_flagged(self):
        hit = collect.check_no_memory_limit(self.wl(resources={"limits": {"memory": "256Mi"}}), context_of())
        self.assertIsNone(hit)

    def test_a_missing_cpu_limit_is_never_flagged(self):
        # Omitting a CPU limit is a deliberate, recommended choice (§3.2).
        hit = collect.check_no_memory_limit(
            self.wl(resources={"limits": {"memory": "256Mi"}, "requests": {"cpu": "1"}}), context_of()
        )
        self.assertIsNone(hit)

    def test_a_limitrange_default_memory_limit_suppresses_it(self):
        limitranges = {"default": [{"spec": {"limits": [{"default": {"memory": "256Mi"}}]}}]}
        hit = collect.check_no_memory_limit(self.wl(resources={}), context_of(limitranges=limitranges))
        self.assertIsNone(hit)

    def test_a_limitrange_defaultRequest_does_not_count_as_a_limit(self):
        # default vs defaultRequest are different LimitRange fields; only
        # `default` backs a memory *limit*.
        limitranges = {"default": [{"spec": {"limits": [{"defaultRequest": {"memory": "256Mi"}}]}}]}
        hit = collect.check_no_memory_limit(self.wl(resources={}), context_of(limitranges=limitranges))
        self.assertIsNotNone(hit)

    def test_no_memory_request_draws_the_first_casualty_arm(self):
        # The kubelet ranks on usage-above-request, so a request of zero puts
        # the leaker in the first group -- the opposite of the sentence that
        # says the node absorbs the leak.
        hit = collect.check_no_memory_limit(self.wl(resources={"requests": {"cpu": "1"}}), context_of())
        self.assertEqual(hit["impact"], collect._IMPACT_NO_LIMIT_UNREQUESTED)

    def test_a_memory_request_draws_the_neighbours_first_arm(self):
        hit = collect.check_no_memory_limit(self.wl(resources={"requests": {"memory": "256Mi"}}), context_of())
        self.assertEqual(hit["impact"], collect._IMPACT_NO_LIMIT_REQUESTED)

    def test_a_zero_memory_request_is_no_request(self):
        # `requests.memory: 0` is what the eviction ranking subtracts, so it
        # reads as zero rather than as a declared reservation.
        hit = collect.check_no_memory_limit(self.wl(resources={"requests": {"memory": "0"}}), context_of())
        self.assertEqual(hit["impact"], collect._IMPACT_NO_LIMIT_UNREQUESTED)

    def test_a_limitrange_defaultRequest_makes_the_request_real(self):
        # Injected before the scheduler sees the pod, so the ranking reads it
        # even though the manifest is silent.
        limitranges = {"default": [{"spec": {"limits": [{"defaultRequest": {"memory": "256Mi"}}]}}]}
        hit = collect.check_no_memory_limit(self.wl(resources={}), context_of(limitranges=limitranges))
        self.assertEqual(hit["impact"], collect._IMPACT_NO_LIMIT_REQUESTED)

    def test_a_workload_matching_both_arms_reports_both_against_their_containers(self):
        d = deployment("api")
        d["spec"]["template"]["spec"]["containers"] = [
            {"name": "app", "resources": {}},
            {"name": "sidecar", "resources": {"requests": {"memory": "64Mi"}}},
        ]
        hit = collect.check_no_memory_limit(collect.normalize_workloads(dump_of(d))[0], context_of())
        self.assertEqual(hit["excerpt"], "containers missing a memory limit: app, sidecar")
        self.assertEqual(
            hit["impact"],
            f"app: {collect._IMPACT_NO_LIMIT_UNREQUESTED} sidecar: {collect._IMPACT_NO_LIMIT_REQUESTED}",
        )


class TestSelectorMatches(unittest.TestCase):
    def test_matchLabels_all_must_match(self):
        self.assertTrue(collect.selector_matches({"matchLabels": {"app": "api"}}, {"app": "api", "tier": "web"}))
        self.assertFalse(collect.selector_matches({"matchLabels": {"app": "api", "tier": "db"}}, {"app": "api"}))

    def test_an_empty_selector_matches_everything(self):
        # The exact footgun 3.3's remediation guards against emitting -- this
        # function reads a live selector faithfully, it does not guard here.
        self.assertTrue(collect.selector_matches({}, {"anything": "goes"}))

    def test_matchExpressions_in_and_not_in(self):
        sel = {"matchExpressions": [{"key": "env", "operator": "In", "values": ["prod", "staging"]}]}
        self.assertTrue(collect.selector_matches(sel, {"env": "prod"}))
        self.assertFalse(collect.selector_matches(sel, {"env": "dev"}))
        sel = {"matchExpressions": [{"key": "env", "operator": "NotIn", "values": ["dev"]}]}
        self.assertFalse(collect.selector_matches(sel, {"env": "dev"}))

    def test_matchExpressions_exists_and_does_not_exist(self):
        self.assertTrue(
            collect.selector_matches({"matchExpressions": [{"key": "app", "operator": "Exists"}]}, {"app": "x"})
        )
        self.assertFalse(
            collect.selector_matches({"matchExpressions": [{"key": "app", "operator": "Exists"}]}, {})
        )
        self.assertFalse(
            collect.selector_matches({"matchExpressions": [{"key": "app", "operator": "DoesNotExist"}]}, {"app": "x"})
        )

    def test_matchLabels_and_matchExpressions_are_anded_together(self):
        sel = {"matchLabels": {"app": "api"}, "matchExpressions": [{"key": "tier", "operator": "In", "values": ["web"]}]}
        self.assertTrue(collect.selector_matches(sel, {"app": "api", "tier": "web"}))
        self.assertFalse(collect.selector_matches(sel, {"app": "api", "tier": "db"}))


def pdb(name, ns="default", selector=None, max_unavailable=None, min_available=None):
    spec = {"selector": selector if selector is not None else {"matchLabels": {"app": "api"}}}
    if max_unavailable is not None:
        spec["maxUnavailable"] = max_unavailable
    if min_available is not None:
        spec["minAvailable"] = min_available
    return {"kind": "PodDisruptionBudget", "metadata": {"namespace": ns, "name": name}, "spec": spec}


def hpa(name, ns="default", min_replicas=1, max_replicas=5, target=None, owned=False):
    doc = {
        "kind": "HorizontalPodAutoscaler",
        "metadata": {"namespace": ns, "name": name},
        "spec": {
            "minReplicas": min_replicas,
            "maxReplicas": max_replicas,
            "scaleTargetRef": target or {"apiVersion": "apps/v1", "kind": "Deployment", "name": "api"},
        },
    }
    if owned:
        doc["metadata"]["ownerReferences"] = [{"kind": "ScaledObject", "name": "x"}]
    return doc


def service(name, ns="default", selector=None, svc_type="ClusterIP", ports=None):
    spec = {"type": svc_type}
    if selector is not None:
        spec["selector"] = selector
    if ports is not None:
        spec["ports"] = ports
    return {"kind": "Service", "metadata": {"namespace": ns, "name": name}, "spec": spec}


class TestNoPdb(unittest.TestCase):
    def wl(self, kind="Deployment", replicas=2, labels=None):
        d = deployment("api", **{"spec.replicas": replicas})
        d["kind"] = kind
        if labels is not None:
            d["spec"]["template"]["metadata"] = {"labels": labels}
        return collect.normalize_workloads(dump_of(d))[0]

    def test_multi_replica_with_no_matching_pdb_is_flagged(self):
        hit = collect.check_no_pdb(self.wl(), context_of())
        self.assertIsNotNone(hit)

    def test_a_matching_pdb_suppresses_it(self):
        workload = self.wl(labels={"app": "api"})
        ctx = context_of(pdbs={"default": [pdb("p", selector={"matchLabels": {"app": "api"}})]})
        self.assertIsNone(collect.check_no_pdb(workload, ctx))

    def test_a_pdb_with_a_non_matching_selector_does_not_help(self):
        workload = self.wl(labels={"app": "api"})
        ctx = context_of(pdbs={"default": [pdb("p", selector={"matchLabels": {"app": "other"}})]})
        self.assertIsNotNone(collect.check_no_pdb(workload, ctx))

    def test_a_daemonset_is_never_flagged(self):
        self.assertIsNone(collect.check_no_pdb(self.wl(kind="DaemonSet"), context_of()))

    def test_a_single_replica_workload_is_never_flagged(self):
        self.assertIsNone(collect.check_no_pdb(self.wl(replicas=1), context_of()))


class TestBlockingPdb(unittest.TestCase):
    def ctx(self, pdb_entry, replicas=3, labels=None, hpas=None, ns="default"):
        # pdb()'s default selector is {"matchLabels": {"app": "api"}}, so the
        # workload needs that label by default too, or every "blocking"
        # fixture below fails to match and asserts nothing.
        d = deployment("api", ns=ns, **{"spec.replicas": replicas})
        d["spec"]["template"]["metadata"] = {"labels": labels if labels is not None else {"app": "api"}}
        workloads = collect.normalize_workloads(dump_of(d))
        return context_of(
            pdbs={ns: [pdb_entry]},
            workloads=workloads,
            hpas={ns: hpas} if hpas is not None else {},
        )

    def test_max_unavailable_zero_is_blocking(self):
        hits = collect.check_blocking_pdb(self.ctx(pdb("p", max_unavailable=0)))
        self.assertEqual(len(hits), 1)
        self.assertIn("PodDisruptionBudget/p", hits[0]["object"])

    def test_max_unavailable_zero_percent_is_blocking(self):
        hits = collect.check_blocking_pdb(self.ctx(pdb("p", max_unavailable="0%")))
        self.assertEqual(len(hits), 1)

    def test_min_available_100_percent_is_blocking(self):
        hits = collect.check_blocking_pdb(self.ctx(pdb("p", min_available="100%")))
        self.assertEqual(len(hits), 1)

    def test_min_available_integer_at_or_above_replicas_is_blocking(self):
        hits = collect.check_blocking_pdb(self.ctx(pdb("p", min_available=3), replicas=3))
        self.assertEqual(len(hits), 1)

    def test_min_available_below_replica_count_is_not_blocking(self):
        hits = collect.check_blocking_pdb(self.ctx(pdb("p", min_available=1), replicas=3))
        self.assertEqual(hits, [])

    def test_a_workload_scaled_to_zero_is_never_blocked(self):
        # S5 drops it in `normalize_workloads`, so the budget matches nothing
        # and is left alone as an orphan would be.
        ctx = self.ctx(pdb("p", max_unavailable=0), replicas=0)
        self.assertEqual(ctx["workloads"], [])
        self.assertEqual(collect.check_blocking_pdb(ctx), [])

    def test_a_budget_over_a_daemonset_is_never_blocking(self):
        # Drains delete DaemonSet pods rather than evict them, so the budget
        # wedges nothing -- and with no `replicas`, `minAvailable: 1` read the
        # one pod as the whole floor.
        d = deployment("agent")
        d["kind"] = "DaemonSet"
        d["spec"].pop("replicas", None)
        d["spec"]["template"]["metadata"] = {"labels": {"app": "api"}}
        ctx = context_of(pdbs={"default": [pdb("p", min_available=1)]}, workloads=collect.normalize_workloads(dump_of(d)))
        self.assertEqual(collect.check_blocking_pdb(ctx), [])

    def test_daemonset_pods_beside_a_deployment_are_slack_the_floor_cannot_count(self):
        # The disruption controller counts the DaemonSet's pods too, so
        # `minAvailable: 2` over two replicas plus a DaemonSet permits
        # evictions; its pod count is unread, so the minAvailable arm is left
        # undecided rather than filed `critical`.
        ds = deployment("agent")
        ds["kind"] = "DaemonSet"
        ds["spec"].pop("replicas", None)
        ds["spec"]["template"]["metadata"] = {"labels": {"app": "api"}}
        api = deployment("api")
        api["spec"]["template"]["metadata"] = {"labels": {"app": "api"}}
        workloads = collect.normalize_workloads(dump_of(api, ds))
        ctx = context_of(pdbs={"default": [pdb("p", min_available=2)]}, workloads=workloads)
        self.assertEqual(collect.check_blocking_pdb(ctx), [])
        ctx = context_of(pdbs={"default": [pdb("p", max_unavailable=0)]}, workloads=workloads)
        hits = collect.check_blocking_pdb(ctx)
        self.assertEqual([h["object"] for h in hits], ["PodDisruptionBudget/p"])

    def test_a_daemonset_beside_a_deployment_leaves_maxunavailable_and_percentages_uncountable(self):
        # The disruption controller needs a scale subresource on every
        # selected pod's controller to resolve these spellings; a DaemonSet has
        # none, so the budget permits no evictions however loose it reads.
        ds = deployment("agent")
        ds["kind"] = "DaemonSet"
        ds["spec"].pop("replicas", None)
        ds["spec"]["template"]["metadata"] = {"labels": {"app": "api"}}
        api = deployment("api", **{"spec.replicas": 5})
        api["spec"]["template"]["metadata"] = {"labels": {"app": "api"}}
        workloads = collect.normalize_workloads(dump_of(api, ds))
        for entry in (pdb("p", max_unavailable=1), pdb("p", max_unavailable="50%"), pdb("p", min_available="20%")):
            with self.subTest(spec=entry["spec"]):
                hits = collect.check_blocking_pdb(context_of(pdbs={"default": [entry]}, workloads=workloads))
                self.assertEqual([h["object"] for h in hits], ["PodDisruptionBudget/p"])
                self.assertIn("DaemonSet agent, which has no scale subresource", hits[0]["excerpt"])

    def test_maxunavailable_one_without_a_daemonset_is_not_blocking(self):
        self.assertEqual(collect.check_blocking_pdb(self.ctx(pdb("p", max_unavailable=1))), [])

    def test_an_orphan_pdb_matching_no_workload_is_not_reported(self):
        ctx = context_of(pdbs={"default": [pdb("p", max_unavailable=0, selector={"matchLabels": {"app": "nope"}})]},
                          workloads=collect.normalize_workloads(dump_of(deployment("api"))))
        self.assertEqual(collect.check_blocking_pdb(ctx), [])

    def test_min_available_below_live_replicas_but_at_the_hpa_floor_is_blocking(self):
        # The whole point of the floor: five replicas now, two at 03:00, and a
        # minAvailable of 2 refuses every eviction once the autoscaler gets
        # there. Comparing against spec.replicas alone reports nothing.
        ctx = self.ctx(pdb("p", min_available=2), replicas=5, hpas=[hpa("h", min_replicas=2)])
        hits = collect.check_blocking_pdb(ctx)
        self.assertEqual(len(hits), 1)
        self.assertIn("minReplicas 2", hits[0]["excerpt"])
        self.assertIn("replicas=5 now", hits[0]["excerpt"])

    def test_min_available_below_the_hpa_floor_is_not_blocking(self):
        ctx = self.ctx(pdb("p", min_available=2), replicas=5, hpas=[hpa("h", min_replicas=3)])
        self.assertEqual(collect.check_blocking_pdb(ctx), [])

    def test_an_absent_hpa_floor_defaults_to_one(self):
        autoscaler = hpa("h")
        del autoscaler["spec"]["minReplicas"]
        ctx = self.ctx(pdb("p", min_available=1), replicas=4, hpas=[autoscaler])
        hits = collect.check_blocking_pdb(ctx)
        self.assertEqual(len(hits), 1)
        self.assertIn("minReplicas 1", hits[0]["excerpt"])

    def test_an_hpa_pointing_elsewhere_leaves_the_declared_count_in_charge(self):
        elsewhere = {"apiVersion": "apps/v1", "kind": "Deployment", "name": "other"}
        ctx = self.ctx(pdb("p", min_available=2), replicas=5, hpas=[hpa("h", min_replicas=2, target=elsewhere)])
        self.assertEqual(collect.check_blocking_pdb(ctx), [])

    def test_a_pdb_in_a_system_namespace_is_never_reported(self):
        ctx = self.ctx(pdb("p", ns="kube-system", max_unavailable=0), ns="kube-system")
        self.assertEqual(collect.check_blocking_pdb(ctx), [])

    def test_an_addon_managed_pdb_over_an_ordinary_workload_is_skipped(self):
        # S2 on the budget, not on the workload: normalize_workloads reads the
        # marker off the Deployment, and this Deployment does not carry it.
        entry = pdb("p", max_unavailable=0)
        entry["metadata"]["labels"] = {"addonmanager.kubernetes.io/mode": "Reconcile"}
        self.assertEqual(collect.check_blocking_pdb(self.ctx(entry)), [])

    def test_a_percentage_that_rounds_up_to_the_whole_count_is_blocking(self):
        # ceil(75% of 3) is 3, so this permits nothing -- the same hard block as
        # minAvailable: 3, in the spelling that reads proportional.
        hits = collect.check_blocking_pdb(self.ctx(pdb("p", min_available="75%"), replicas=3))
        self.assertEqual(len(hits), 1)
        self.assertIn("desiredHealthy 3", hits[0]["excerpt"])
        self.assertIn("0 evictions permitted", hits[0]["excerpt"])

    def test_a_percentage_that_leaves_room_is_not_blocking(self):
        # ceil(50% of 4) is 2, leaving two evictions.
        self.assertEqual(collect.check_blocking_pdb(self.ctx(pdb("p", min_available="50%"), replicas=4)), [])

    def test_any_percentage_over_a_floor_of_one_is_blocking(self):
        # ceil of any non-zero fraction of 1 is 1, so even "1%" refuses the only
        # pod there is. This is the shape a chart's default produces.
        hits = collect.check_blocking_pdb(self.ctx(pdb("p", min_available="1%"), replicas=1))
        self.assertEqual(len(hits), 1)
        self.assertIn("desiredHealthy 1", hits[0]["excerpt"])

    def test_zero_percent_permits_everything(self):
        self.assertEqual(collect.check_blocking_pdb(self.ctx(pdb("p", min_available="0%"), replicas=3)), [])

    def test_a_percentage_is_resolved_against_the_hpa_floor(self):
        # 50% of the five replicas running now is 3 of 5 and looks safe; 50% of
        # the floor of 2 is 1 of 2, still safe. 60% of 2 is ceil(1.2) = 2, which
        # is not. The floor is what decides, and only the floor.
        safe = self.ctx(pdb("p", min_available="50%"), replicas=5, hpas=[hpa("h", min_replicas=2)])
        self.assertEqual(collect.check_blocking_pdb(safe), [])
        blocked = self.ctx(pdb("p", min_available="60%"), replicas=5, hpas=[hpa("h", min_replicas=2)])
        hits = collect.check_blocking_pdb(blocked)
        self.assertEqual(len(hits), 1)
        self.assertIn("minAvailable 60% of 2 resolves to desiredHealthy 2", hits[0]["excerpt"])

    def test_a_malformed_percentage_falls_through_rather_than_raising(self):
        # Not a shape the apiserver accepts, so there is nothing to resolve; the
        # point is that it does not take the collector down with a ValueError.
        self.assertEqual(collect.check_blocking_pdb(self.ctx(pdb("p", min_available="7.5%"), replicas=3)), [])

    def test_a_percentage_over_a_workload_scaled_to_zero_is_not_reported(self):
        # Nothing to evict. S5 drops the workload before the check runs, so
        # the percentage never meets a floor of 0 -- where ceil(x * 0 / 100)
        # would be 0, which is >= 0, and read as blocking.
        ctx = self.ctx(pdb("p", min_available="100%"), replicas=0)
        self.assertEqual(ctx["workloads"], [])
        self.assertEqual(collect.check_blocking_pdb(ctx), [])


    def _two_deployments(self, pdb_entry, replicas=2):
        items = []
        for name in ("a", "b"):
            d = deployment(name, **{"spec.replicas": replicas})
            d["spec"]["template"]["metadata"] = {"labels": {"app": "api"}}
            items.append(d)
        return context_of(pdbs={"default": [pdb_entry]}, workloads=collect.normalize_workloads(dump_of(*items)))

    def test_a_shared_pdb_is_measured_against_every_workload_it_selects(self):
        # Two Deployments of two under one minAvailable: 2 leave two evictions.
        self.assertEqual(collect.check_blocking_pdb(self._two_deployments(pdb("p", min_available=2))), [])
        self.assertEqual(collect.check_blocking_pdb(self._two_deployments(pdb("p", min_available="60%"))), [])

    def test_a_shared_pdb_that_does_block_names_every_workload(self):
        hits = collect.check_blocking_pdb(self._two_deployments(pdb("p", min_available=4)))
        self.assertEqual(len(hits), 1)
        self.assertIn("Deployment/b", hits[0]["excerpt"])
        self.assertIn("4 pods selected", hits[0]["excerpt"])

class TestPdbOverlapping(unittest.TestCase):
    def ctx(self, *pdb_entries, labels=None, ns="default", replicas=3):
        d = deployment("api", ns=ns, **{"spec.replicas": replicas})
        d["spec"]["template"]["metadata"] = {"labels": labels if labels is not None else {"app": "api"}}
        return context_of(
            pdbs={ns: list(pdb_entries)},
            workloads=collect.normalize_workloads(dump_of(d)),
        )

    def test_two_budgets_over_one_workload_is_flagged(self):
        ctx = self.ctx(pdb("chart-pdb", max_unavailable=1), pdb("hand-written", max_unavailable=1))
        hits = collect.check_pdb_overlapping(ctx)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "Deployment/api")
        self.assertIn("chart-pdb, hand-written", hits[0]["excerpt"])
        self.assertIn("2 PodDisruptionBudgets", hits[0]["excerpt"])

    def test_one_budget_is_the_norm_and_is_not_flagged(self):
        self.assertEqual(collect.check_pdb_overlapping(self.ctx(pdb("only", max_unavailable=1))), [])

    def test_a_second_budget_selecting_something_else_is_not_an_overlap(self):
        elsewhere = pdb("other", max_unavailable=1, selector={"matchLabels": {"app": "worker"}})
        self.assertEqual(collect.check_pdb_overlapping(self.ctx(pdb("mine", max_unavailable=1), elsewhere)), [])

    def test_an_empty_selector_swallows_the_namespace_and_overlaps(self):
        # policy/v1: `selector: {}` matches every pod in the namespace. This is
        # the shape 3.3's remediation is forbidden from emitting, and the way a
        # namespace-wide budget silently overlaps every workload in it.
        catch_all = pdb("catch-all", max_unavailable=1, selector={})
        hits = collect.check_pdb_overlapping(self.ctx(pdb("mine", max_unavailable=1), catch_all))
        self.assertEqual(len(hits), 1)

    def test_an_absent_selector_selects_nothing_and_does_not_overlap(self):
        # The other half of the same distinction: null selects no pods, so
        # collapsing it to {} the way selector_matches does would make every
        # workload in a namespace holding one real budget read as covered twice.
        null = pdb("null-selector", max_unavailable=1)
        del null["spec"]["selector"]
        self.assertEqual(collect.check_pdb_overlapping(self.ctx(pdb("mine", max_unavailable=1), null)), [])

    def test_an_addon_budget_still_counts_toward_the_overlap(self):
        # S2 suppresses a finding about an object nobody can edit; here the
        # editable half is the user's budget, and the finding is the pair.
        addon = pdb("gke-addon", max_unavailable=1)
        addon["metadata"]["labels"] = {"addonmanager.kubernetes.io/mode": "Reconcile"}
        hits = collect.check_pdb_overlapping(self.ctx(pdb("mine", max_unavailable=1), addon))
        self.assertEqual(len(hits), 1)
        self.assertIn("gke-addon is addon-managed", hits[0]["excerpt"])

    def test_an_overlap_of_addon_budgets_alone_is_not_reported(self):
        entries = []
        for name in ("addon-a", "addon-b"):
            entry = pdb(name, max_unavailable=1)
            entry["metadata"]["labels"] = {"addonmanager.kubernetes.io/mode": "Reconcile"}
            entries.append(entry)
        self.assertEqual(collect.check_pdb_overlapping(self.ctx(*entries)), [])

    def test_a_system_namespace_is_never_reported(self):
        ctx = self.ctx(pdb("a", ns="kube-system"), pdb("b", ns="kube-system"), ns="kube-system")
        self.assertEqual(collect.check_pdb_overlapping(ctx), [])

    def test_three_budgets_report_once_naming_all_three(self):
        ctx = self.ctx(pdb("c"), pdb("a"), pdb("b"))
        hits = collect.check_pdb_overlapping(ctx)
        self.assertEqual(len(hits), 1)
        self.assertIn("3 PodDisruptionBudgets: a, b, c", hits[0]["excerpt"])

    def test_two_overlapped_workloads_report_separately(self):
        # One catch-all budget beside two per-workload ones: two findings, each
        # naming its own workload, because the fix may differ between them.
        api = deployment("api", **{"spec.replicas": 2})
        api["spec"]["template"]["metadata"] = {"labels": {"app": "api"}}
        worker = deployment("worker", **{"spec.replicas": 2})
        worker["spec"]["template"]["metadata"] = {"labels": {"app": "worker"}}
        ctx = context_of(
            pdbs={
                "default": [
                    pdb("catch-all", selector={}),
                    pdb("api-pdb", selector={"matchLabels": {"app": "api"}}),
                    pdb("worker-pdb", selector={"matchLabels": {"app": "worker"}}),
                ]
            },
            workloads=collect.normalize_workloads(dump_of(api, worker)),
        )
        hits = collect.check_pdb_overlapping(ctx)
        self.assertEqual([h["object"] for h in hits], ["Deployment/api", "Deployment/worker"])


class TestNoHpa(unittest.TestCase):
    def wl(self, replicas=3, kind="Deployment"):
        d = deployment("api", **{"spec.replicas": replicas})
        d["kind"] = kind
        return collect.normalize_workloads(dump_of(d))[0]

    def test_three_or_more_replicas_with_no_hpa_is_flagged(self):
        self.assertIsNotNone(collect.check_no_hpa(self.wl(), context_of()))

    def test_a_matching_hpa_suppresses_it(self):
        ctx = context_of(hpas={"default": [hpa("h")]})
        self.assertIsNone(collect.check_no_hpa(self.wl(), ctx))

    def test_fewer_than_three_replicas_is_never_flagged(self):
        self.assertIsNone(collect.check_no_hpa(self.wl(replicas=2), context_of()))

    def test_a_statefulset_is_never_flagged(self):
        self.assertIsNone(collect.check_no_hpa(self.wl(kind="StatefulSet"), context_of()))

    def test_a_keda_owned_hpa_still_counts_because_it_is_a_real_hpa(self):
        # A KEDA-scaled Deployment is autoscaled. Dropping owned HPAs at the
        # context told 3.5 otherwise and filed "add an HPA" against a workload
        # KEDA already scales; only the checks that grade the HPA's own
        # min/max (3.6, 3.22) skip it.
        ctx = context_of()
        ctx["hpas"] = collect.hpas_by_namespace(dump_of(hpa("h", owned=True)))
        self.assertEqual(len(ctx["hpas"]["default"]), 1)
        self.assertIsNone(collect.check_no_hpa(self.wl(), ctx))


class TestHpaCannotScale(unittest.TestCase):
    def test_min_equals_max_is_pinned_major(self):
        ctx = context_of(hpas={"default": [hpa("h", min_replicas=3, max_replicas=3)]})
        hits = collect.check_hpa_cannot_scale(ctx)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "major")

    def test_a_dangling_target_is_minor(self):
        workloads = collect.normalize_workloads(dump_of(deployment("other")))
        ctx = context_of(
            hpas={"default": [hpa("h", target={"apiVersion": "apps/v1", "kind": "Deployment", "name": "gone"})]},
            workloads=workloads,
        )
        hits = collect.check_hpa_cannot_scale(ctx)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "minor")

    def test_a_healthy_hpa_with_an_existing_target_is_not_flagged(self):
        workloads = collect.normalize_workloads(dump_of(deployment("api")))
        ctx = context_of(hpas={"default": [hpa("h", min_replicas=1, max_replicas=5)]}, workloads=workloads)
        self.assertEqual(collect.check_hpa_cannot_scale(ctx), [])

    def test_a_target_kind_outside_the_dump_is_not_dangling(self):
        # The cluster was readable; an unevaluated target (e.g. a
        # StatefulSet the dump does carry, or a custom resource) belongs in
        # limitations prose, never in this finding.
        ctx = context_of(hpas={"default": [hpa("h", target={"apiVersion": "apps/v1", "kind": "CustomThing", "name": "x"})]})
        self.assertEqual(collect.check_hpa_cannot_scale(ctx), [])

    def test_a_gke_managed_namespace_hpa_is_not_flagged(self):
        # GKE puts kube-state-metrics in `gke-managed-cim`. S1 keeps its
        # StatefulSet out of `workloads`, so before the HPA carried the same
        # suppression this read as a dangling target on every cluster in the
        # fleet -- 17 minor findings about objects Google owns.
        dump = dump_of(
            deployment("kube-state-metrics", ns="gke-managed-cim"),
            hpa("kube-state-metrics", ns="gke-managed-cim",
                target={"apiVersion": "apps/v1", "kind": "Deployment", "name": "kube-state-metrics"}),
        )
        self.assertEqual(collect.check_hpa_cannot_scale(context_of(dump)), [])

    def test_a_gke_managed_namespace_pinned_hpa_is_not_flagged_either(self):
        # The pinned branch needs the suppression as much as the dangling one:
        # an addon HPA Google pinned is not the operator's to widen.
        dump = dump_of(hpa("otel", ns="gke-managed-otel", min_replicas=2, max_replicas=2))
        self.assertEqual(collect.check_hpa_cannot_scale(context_of(dump)), [])

    def test_an_addonmanager_labelled_hpa_is_not_flagged(self):
        # S2, for the addon that sits in a namespace S1 does not cover.
        h = hpa("addon", min_replicas=2, max_replicas=2)
        h["metadata"]["labels"] = {"addonmanager.kubernetes.io/mode": "Reconcile"}
        self.assertEqual(collect.check_hpa_cannot_scale(context_of(dump_of(h))), [])

    def test_an_exempted_target_still_exists_so_the_hpa_is_not_dangling(self):
        # S4 takes the Deployment out of the audited set. "scaleTargetRef
        # Deployment/api not found" would be a false statement about the
        # cluster, and opting a workload out of the audit would create a
        # finding rather than remove one.
        dep = deployment("api")
        dep["metadata"]["labels"] = {collect.OPT_OUT_KEY: "exempt"}
        ctx = context_of(dump_of(dep, hpa("h")))
        self.assertEqual(ctx["workloads"], [])
        self.assertEqual(collect.check_hpa_cannot_scale(ctx), [])

    def test_a_scaled_to_zero_target_still_exists(self):
        # S5, the same mistake from the other side.
        ctx = context_of(dump_of(deployment("api", **{"spec.replicas": 0}), hpa("h")))
        self.assertEqual(ctx["workloads"], [])
        self.assertEqual(collect.check_hpa_cannot_scale(ctx), [])

    def test_a_genuinely_absent_target_is_still_dangling(self):
        # The whole point of the check survives the two fixes above.
        ctx = context_of(dump_of(deployment("other"),
                                 hpa("h", target={"apiVersion": "apps/v1", "kind": "Deployment", "name": "gone"})))
        hits = collect.check_hpa_cannot_scale(ctx)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "minor")
        self.assertIn("Deployment/gone not found", hits[0]["excerpt"])


class TestHpaDanglingGuards(unittest.TestCase):
    def test_a_target_in_another_api_group_is_not_dangling(self):
        # A `Deployment` from some CRD group is not an apps/v1 Deployment the
        # dump could have carried.
        ctx = context_of(hpas={"default": [hpa("h", target={"apiVersion": "example.com/v1", "kind": "Deployment", "name": "gone"})]})
        self.assertEqual(collect.check_hpa_cannot_scale(ctx), [])

    def test_an_hpa_the_controller_reports_able_to_scale_is_not_dangling(self):
        h = hpa("h", target={"apiVersion": "apps/v1", "kind": "Deployment", "name": "gone"})
        h["status"] = {"conditions": [{"type": "AbleToScale", "status": "True"}]}
        self.assertEqual(collect.check_hpa_cannot_scale(context_of(hpas={"default": [h]})), [])

    def test_an_hpa_the_controller_cannot_resolve_is_dangling(self):
        h = hpa("h", target={"apiVersion": "apps/v1", "kind": "Deployment", "name": "gone"})
        h["status"] = {"conditions": [{"type": "AbleToScale", "status": "False", "reason": "FailedGetScale"}]}
        self.assertEqual(len(collect.check_hpa_cannot_scale(context_of(hpas={"default": [h]}))), 1)


class TestHpaFloorsAtOne(unittest.TestCase):
    """§3.22 — the floor, not the instantaneous replica count."""

    def ctx(self, *, min_replicas=1, max_replicas=5, replicas=3, ns="default",
            strategy=None, selector=None, ports=None, hpa_kw=None, target=None):
        d = deployment("api", ns=ns, **{"spec.replicas": replicas})
        d["spec"]["template"]["metadata"] = {"labels": {"app": "api"}}
        if strategy is not None:
            d["spec"]["strategy"] = {"type": strategy}
        docs = [d]
        h = hpa("h", ns=ns, min_replicas=min_replicas, max_replicas=max_replicas,
                target=target or {"apiVersion": "apps/v1", "kind": "Deployment", "name": "api"},
                **(hpa_kw or {}))
        if min_replicas is None:
            del h["spec"]["minReplicas"]
        docs.append(h)
        if selector is not False:
            docs.append(service("s", ns=ns, selector=selector or {"app": "api"},
                                ports=ports if ports is not None else [{"name": "http", "port": 80}]))
        return context_of(dump_of(*docs))

    def test_a_floor_of_one_behind_a_service_is_flagged_on_the_hpa(self):
        hits = collect.check_hpa_floors_at_one(self.ctx())
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "HorizontalPodAutoscaler/h")
        self.assertEqual(hits[0]["namespace"], "default")
        self.assertIn("minReplicas 1", hits[0]["excerpt"])
        self.assertIn("scaling Deployment/api", hits[0]["excerpt"])
        self.assertIn("(serving traffic)", hits[0]["excerpt"])

    def test_the_instantaneous_replica_count_does_not_suppress_it(self):
        """The whole point of the check.

        §3.11 reads `spec.replicas` and stays quiet at three. The 03:00
        rollout does not happen at three -- it happens at the floor, which is
        one, and this check has to fire whatever the dump caught the
        autoscaler doing.
        """
        for replicas in (1, 3, 12):
            with self.subTest(replicas=replicas):
                hits = collect.check_hpa_floors_at_one(self.ctx(replicas=replicas))
                self.assertEqual(len(hits), 1)
                self.assertIn(f"spec.replicas reads now ({replicas})", hits[0]["excerpt"])

    def test_an_absent_min_replicas_is_the_api_default_and_says_so(self):
        hits = collect.check_hpa_floors_at_one(self.ctx(min_replicas=None))
        self.assertEqual(len(hits), 1)
        self.assertIn("minReplicas 1 (unset, so the API default)", hits[0]["excerpt"])

    def test_a_declared_floor_is_not_labelled_as_defaulted(self):
        hits = collect.check_hpa_floors_at_one(self.ctx(min_replicas=1))
        self.assertNotIn("unset", hits[0]["excerpt"])

    def test_a_floor_above_one_is_not_flagged(self):
        self.assertEqual(collect.check_hpa_floors_at_one(self.ctx(min_replicas=2)), [])

    def test_a_ceiling_of_one_belongs_to_hpa_cannot_scale(self):
        # min == max == 1 is that check's pinned arm at `major`, and asking for
        # a floor of 2 under a ceiling of 1 is rejected by the API.
        ctx = self.ctx(min_replicas=1, max_replicas=1)
        self.assertEqual(collect.check_hpa_floors_at_one(ctx), [])
        self.assertEqual(len(collect.check_hpa_cannot_scale(ctx)), 1)

    def test_the_two_hpa_checks_never_both_fire(self):
        ctx = self.ctx(min_replicas=1, max_replicas=5)
        self.assertEqual(len(collect.check_hpa_floors_at_one(ctx)), 1)
        self.assertEqual(collect.check_hpa_cannot_scale(ctx), [])

    def test_a_target_no_service_selects_is_not_flagged(self):
        self.assertEqual(collect.check_hpa_floors_at_one(self.ctx(selector=False)), [])

    def test_a_service_that_selects_something_else_does_not_count(self):
        self.assertEqual(collect.check_hpa_floors_at_one(self.ctx(selector={"app": "other"})), [])

    def test_a_metrics_only_service_still_fires_but_says_which_scope(self):
        # The impact differs and the exposure line carries it, exactly as
        # §3.11 and §3.9 do. Suppressing it would hide a real gap in the
        # scrape; claiming user traffic would be false.
        hits = collect.check_hpa_floors_at_one(self.ctx(ports=[{"name": "http-metrics", "port": 9402}]))
        self.assertEqual(len(hits), 1)
        self.assertIn("(metrics scrape only)", hits[0]["excerpt"])

    def test_a_recreate_strategy_is_left_alone(self):
        self.assertEqual(collect.check_hpa_floors_at_one(self.ctx(strategy="Recreate")), [])

    def test_a_gke_managed_namespace_hpa_is_not_flagged(self):
        # S1. Every cluster in the fleet carries `kube-state-metrics` in
        # `gke-managed-cim` with a floor of one behind a Service, and it is
        # not the operator's to raise -- one false finding per cluster.
        self.assertEqual(collect.check_hpa_floors_at_one(self.ctx(ns="gke-managed-cim")), [])

    def test_an_addonmanager_labelled_hpa_is_not_flagged(self):
        # S2, for the addon outside a namespace S1 covers.
        ctx = self.ctx(hpa_kw={})
        ctx["hpas"]["default"][0]["metadata"]["labels"] = {"addonmanager.kubernetes.io/mode": "Reconcile"}
        self.assertEqual(collect.check_hpa_floors_at_one(ctx), [])

    def test_a_dangling_target_belongs_to_hpa_cannot_scale(self):
        ctx = self.ctx(target={"apiVersion": "apps/v1", "kind": "Deployment", "name": "gone"})
        self.assertEqual(collect.check_hpa_floors_at_one(ctx), [])
        self.assertEqual(len(collect.check_hpa_cannot_scale(ctx)), 1)

    def test_a_target_in_another_api_group_is_not_this_workload(self):
        ctx = self.ctx(target={"apiVersion": "acme.io/v1", "kind": "Deployment", "name": "api"})
        self.assertEqual(collect.check_hpa_floors_at_one(ctx), [])

    def test_the_finding_carries_the_hpas_own_reconciler(self):
        # The fix edits the HPA, so a note about what reasserts the Deployment
        # would send the reader to the wrong file.
        ctx = self.ctx()
        ctx["hpas"]["default"][0]["metadata"]["annotations"] = {
            "argocd.argoproj.io/tracking-id": "platform:autoscaling/HorizontalPodAutoscaler:default/h"
        }
        hits = collect.check_hpa_floors_at_one(ctx)
        self.assertIsNotNone(hits[0]["reconciler"])


class TestSingleReplicaDefersToTheHpaCheck(unittest.TestCase):
    """§3.11's new exclusion — the two checks partition one population."""

    def ctx_and_workload(self, *, with_hpa, min_replicas=1):
        d = deployment("api", **{"spec.replicas": 1})
        d["spec"]["template"]["metadata"] = {"labels": {"app": "api"}}
        docs = [d, service("s", selector={"app": "api"}, ports=[{"name": "http", "port": 80}])]
        if with_hpa:
            docs.append(hpa("h", min_replicas=min_replicas, max_replicas=5))
        ctx = context_of(dump_of(*docs))
        return ctx, collect.normalize_workloads(dump_of(d))[0]

    def test_without_an_hpa_it_still_fires(self):
        ctx, wl = self.ctx_and_workload(with_hpa=False)
        self.assertIsNotNone(collect.check_single_replica(wl, ctx))

    def test_an_hpa_hands_the_workload_to_the_floor_check(self):
        ctx, wl = self.ctx_and_workload(with_hpa=True)
        self.assertIsNone(collect.check_single_replica(wl, ctx))
        self.assertEqual(len(collect.check_hpa_floors_at_one(ctx)), 1)

    def test_neither_fires_where_the_hpa_already_floors_above_one(self):
        # `spec.replicas: 1` under `minReplicas: 2` is the autoscaler mid-flight
        # or a stale field; either way nothing here is a finding.
        ctx, wl = self.ctx_and_workload(with_hpa=True, min_replicas=2)
        self.assertIsNone(collect.check_single_replica(wl, ctx))
        self.assertEqual(collect.check_hpa_floors_at_one(ctx), [])


class TestRigidScheduling(unittest.TestCase):
    def wl(self, node_selector=None, affinity=None, kind="Deployment", vct=False):
        d = deployment("api")
        d["kind"] = kind
        if node_selector is not None:
            d["spec"]["template"]["spec"]["nodeSelector"] = node_selector
        if affinity is not None:
            d["spec"]["template"]["spec"]["affinity"] = affinity
        if vct:
            d["spec"]["volumeClaimTemplates"] = [{"metadata": {"name": "data"}}]
        return collect.normalize_workloads(dump_of(d))[0]

    def test_hostname_node_selector_is_critical(self):
        hit = collect.check_rigid_scheduling(self.wl(node_selector={"kubernetes.io/hostname": "node-1"}), context_of())
        self.assertEqual(hit["severity"], "critical")

    def test_single_zone_node_selector_is_major(self):
        hit = collect.check_rigid_scheduling(
            self.wl(node_selector={"topology.kubernetes.io/zone": "us-central1-a"}), context_of()
        )
        self.assertEqual(hit["severity"], "major")

    def test_a_statefulset_with_zonal_storage_is_not_flagged_for_its_zone_pin(self):
        hit = collect.check_rigid_scheduling(
            self.wl(node_selector={"topology.kubernetes.io/zone": "us-central1-a"}, kind="StatefulSet", vct=True),
            context_of(),
        )
        self.assertIsNone(hit)

    def test_a_hardware_selector_is_never_flagged(self):
        hit = collect.check_rigid_scheduling(
            self.wl(node_selector={"cloud.google.com/gke-accelerator": "nvidia-t4"}), context_of()
        )
        self.assertIsNone(hit)

    def test_hostname_node_affinity_is_critical(self):
        affinity = {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": ["node-1"]}]}
                    ]
                }
            }
        }
        hit = collect.check_rigid_scheduling(self.wl(affinity=affinity), context_of())
        self.assertEqual(hit["severity"], "critical")

    def test_a_multi_value_zone_affinity_is_never_flagged(self):
        affinity = {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {"matchExpressions": [{"key": "topology.kubernetes.io/zone", "operator": "In", "values": ["a", "b"]}]}
                    ]
                }
            }
        }
        self.assertIsNone(collect.check_rigid_scheduling(self.wl(affinity=affinity), context_of()))

    def test_preferred_affinity_is_never_flagged(self):
        affinity = {
            "nodeAffinity": {
                "preferredDuringSchedulingIgnoredDuringExecution": [
                    {"preference": {"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": ["x"]}]}}
                ]
            }
        }
        self.assertIsNone(collect.check_rigid_scheduling(self.wl(affinity=affinity), context_of()))

    def test_an_unpinned_workload_is_not_flagged(self):
        self.assertIsNone(collect.check_rigid_scheduling(self.wl(), context_of()))


class TestNoSpread(unittest.TestCase):
    def wl(self, replicas=2, tsc=None, anti_affinity=None, kind="Deployment"):
        d = deployment("api", **{"spec.replicas": replicas})
        d["kind"] = kind
        if tsc is not None:
            d["spec"]["template"]["spec"]["topologySpreadConstraints"] = tsc
        if anti_affinity is not None:
            d["spec"]["template"]["spec"]["affinity"] = {"podAntiAffinity": anti_affinity}
        return collect.normalize_workloads(dump_of(d))[0]

    def test_multi_replica_with_neither_mechanism_is_flagged(self):
        self.assertIsNotNone(collect.check_no_spread(self.wl(), context_of()))

    def test_a_topology_spread_constraint_suppresses_it(self):
        tsc = [{"maxSkew": 1, "topologyKey": "kubernetes.io/hostname", "whenUnsatisfiable": "ScheduleAnyway"}]
        self.assertIsNone(collect.check_no_spread(self.wl(tsc=tsc), context_of()))

    def test_required_pod_anti_affinity_suppresses_it(self):
        anti = {"requiredDuringSchedulingIgnoredDuringExecution": [{"topologyKey": "kubernetes.io/hostname"}]}
        self.assertIsNone(collect.check_no_spread(self.wl(anti_affinity=anti), context_of()))

    def test_preferred_pod_anti_affinity_suppresses_it(self):
        anti = {
            "preferredDuringSchedulingIgnoredDuringExecution": [
                {"podAffinityTerm": {"topologyKey": "topology.kubernetes.io/zone"}}
            ]
        }
        self.assertIsNone(collect.check_no_spread(self.wl(anti_affinity=anti), context_of()))

    def test_a_daemonset_is_never_flagged(self):
        self.assertIsNone(collect.check_no_spread(self.wl(kind="DaemonSet"), context_of()))

    def test_single_replica_is_never_flagged(self):
        self.assertIsNone(collect.check_no_spread(self.wl(replicas=1), context_of()))


class TestSpreadNotAchieved(unittest.TestCase):
    """§3.19. Advisory spreading that demonstrably did not spread.

    The declaration-shape version of this check -- "flag every workload whose
    spreading is advisory" -- is the one that must not ship: §3.8's own
    remediation prescribes `ScheduleAnyway`, so it would re-flag every
    workload §3.8 had just fixed. The observation is what makes it a finding.
    """

    NODE_A = "gke-node-a"
    NODE_B = "gke-node-b"
    ZONE_A = "us-east4-a"
    ADVISORY = [{"maxSkew": 1, "topologyKey": collect._HOSTNAME_KEY, "whenUnsatisfiable": "ScheduleAnyway"}]

    def wl(self, replicas=2, tsc=None, anti_affinity=None, kind="Deployment", name="api"):
        d = deployment(name, **{"spec.replicas": replicas})
        d["kind"] = kind
        d["spec"]["template"]["metadata"] = {"labels": {"app": name}}
        if tsc is not None:
            d["spec"]["template"]["spec"]["topologySpreadConstraints"] = tsc
        if anti_affinity is not None:
            d["spec"]["template"]["spec"]["affinity"] = {"podAntiAffinity": anti_affinity}
        return collect.normalize_workloads(dump_of(d))[0]

    def ctx(self, workload, endpoints=None, seen=None, others=(), services=None, zones=None):
        if endpoints is None:
            endpoints = [self.NODE_A, self.NODE_A]
        if seen is None:
            seen = {self.NODE_A, self.NODE_B}
        if services is None:
            services = [("api-svc", endpoints)]
        if zones is None:
            zones = {self.ZONE_A}
        return context_of(
            service_endpoints=[
                {
                    "ns": workload["ns"],
                    "name": svc,
                    "selector": {"app": workload["name"]},
                    "endpoint_nodes": list(nodes),
                }
                for svc, nodes in services
            ],
            endpoint_nodes_seen=set(seen),
            endpoint_zones_seen=set(zones),
            workloads=[workload, *others],
        )

    def test_the_excerpt_carries_the_counts_the_remediation_branches_on(self):
        """Three edits close this finding and they are not interchangeable.

        A hostname-keyed `DoNotSchedule` pends a replica where the cluster has
        fewer nodes than the workload has replicas, and a zone-keyed one is a
        no-op where every node shares a zone -- it merges, the finding
        survives, and the reviewer paid attention for nothing. Neither count is
        anywhere else in the finding, so on 2026-09-07 the model read the
        three-way choice, saw no way to pick, and wrote `manual` on both hits:
        honest, and still a pull request nobody got.
        """
        wl = self.wl(tsc=self.ADVISORY)
        hit = collect.check_spread_not_achieved(wl, self.ctx(wl))
        self.assertIn("this cluster's EndpointSlices name 2 nodes across 1 zone", hit["excerpt"])

    def test_the_zone_count_is_pluralised(self):
        wl = self.wl(tsc=self.ADVISORY)
        hit = collect.check_spread_not_achieved(wl, self.ctx(wl, zones={self.ZONE_A, "us-east4-b"}))
        self.assertIn("2 nodes across 2 zones", hit["excerpt"])

    def test_a_dump_with_no_zone_labels_omits_the_clause_rather_than_claiming_nought(self):
        # `endpoints[].zone` is optional. Writing "across 0 zones" would read as
        # a fact about the cluster instead of a gap in the dump, and the SOP's
        # `M == 1` branch -- the one that must not produce a manifest -- would
        # fire on it.
        wl = self.wl(tsc=self.ADVISORY)
        hit = collect.check_spread_not_achieved(wl, self.ctx(wl, zones=set()))
        self.assertIn("name 2 nodes", hit["excerpt"])
        self.assertNotIn("zone", hit["excerpt"].split("name 2 nodes")[1])

    def test_advisory_spreading_that_landed_on_one_node_is_flagged(self):
        wl = self.wl(tsc=self.ADVISORY)
        hit = collect.check_spread_not_achieved(wl, self.ctx(wl))
        self.assertIsNotNone(hit)
        self.assertIn(self.NODE_A, hit["excerpt"])
        self.assertIn("all 2 ready endpoints", hit["excerpt"])
        self.assertIn("ScheduleAnyway", hit["excerpt"])

    def test_endpoints_on_two_nodes_are_not_flagged(self):
        """The advisory constraint held. There is nothing to report."""
        wl = self.wl(tsc=self.ADVISORY)
        self.assertIsNone(collect.check_spread_not_achieved(wl, self.ctx(wl, endpoints=[self.NODE_A, self.NODE_B])))

    def test_a_single_node_cluster_is_never_flagged(self):
        """Co-location is forced, so the manifest is not the defect."""
        wl = self.wl(tsc=self.ADVISORY)
        self.assertIsNone(collect.check_spread_not_achieved(wl, self.ctx(wl, seen={self.NODE_A})))

    def test_a_binding_constraint_is_not_flagged_even_when_colocated(self):
        """Something enforced spreading; that it did not is a scheduler
        question, not a manifest one."""
        tsc = [{"topologyKey": collect._HOSTNAME_KEY, "whenUnsatisfiable": "DoNotSchedule"}]
        wl = self.wl(tsc=tsc)
        self.assertIsNone(collect.check_spread_not_achieved(wl, self.ctx(wl)))

    def test_an_absent_when_unsatisfiable_reads_as_binding(self):
        """The API server defaults the field to `DoNotSchedule`."""
        wl = self.wl(tsc=[{"topologyKey": collect._HOSTNAME_KEY}])
        self.assertIsNone(collect.check_spread_not_achieved(wl, self.ctx(wl)))

    def test_one_binding_rule_beside_advisory_ones_suppresses_it(self):
        tsc = self.ADVISORY + [{"topologyKey": collect._ZONE_KEY, "whenUnsatisfiable": "DoNotSchedule"}]
        wl = self.wl(tsc=tsc)
        self.assertIsNone(collect.check_spread_not_achieved(wl, self.ctx(wl)))

    def test_required_anti_affinity_beside_an_advisory_constraint_suppresses_it(self):
        anti = {"requiredDuringSchedulingIgnoredDuringExecution": [{"topologyKey": collect._ZONE_KEY}]}
        wl = self.wl(tsc=self.ADVISORY, anti_affinity=anti)
        self.assertIsNone(collect.check_spread_not_achieved(wl, self.ctx(wl)))

    def test_preferred_anti_affinity_alone_counts_as_advisory(self):
        anti = {"preferredDuringSchedulingIgnoredDuringExecution": [{"podAffinityTerm": {"topologyKey": collect._ZONE_KEY}}]}
        wl = self.wl(anti_affinity=anti)
        hit = collect.check_spread_not_achieved(wl, self.ctx(wl))
        self.assertIsNotNone(hit)
        self.assertIn("preferredDuringScheduling", hit["excerpt"])

    def test_anti_affinity_on_an_unrelated_topology_key_is_not_spreading(self):
        anti = {"preferredDuringSchedulingIgnoredDuringExecution": [{"podAffinityTerm": {"topologyKey": "rack"}}]}
        wl = self.wl(anti_affinity=anti)
        self.assertIsNone(collect.check_spread_not_achieved(wl, self.ctx(wl)))

    def test_declaring_nothing_at_all_is_left_to_no_spread(self):
        """§3.8 owns the empty case; firing here too would put two verdicts on
        one object in one ledger."""
        wl = self.wl()
        self.assertIsNone(collect.check_spread_not_achieved(wl, self.ctx(wl)))
        self.assertIsNotNone(collect.check_no_spread(wl, context_of()))

    def test_one_endpoint_is_not_co_location(self):
        wl = self.wl(tsc=self.ADVISORY)
        self.assertIsNone(collect.check_spread_not_achieved(wl, self.ctx(wl, endpoints=[self.NODE_A])))

    def test_no_endpoints_at_all_produce_no_finding(self):
        """A workload behind no Service leaves no evidence, so it is out of
        scope rather than clean -- the SOP says so under coverage."""
        wl = self.wl(tsc=self.ADVISORY)
        self.assertIsNone(collect.check_spread_not_achieved(wl, self.ctx(wl, services=[])))

    def test_a_service_shared_with_another_workload_is_not_evidence(self):
        """Its slices mix two workloads' pods, so co-location would be a claim
        about the pair rather than about the object the finding names."""
        wl = self.wl(tsc=self.ADVISORY)
        other = self.wl(name="api")
        other["kind"], other["name"] = "StatefulSet", "api-sts"
        other["pod_labels"] = {"app": "api"}
        self.assertIsNone(collect.check_spread_not_achieved(wl, self.ctx(wl, others=[other])))

    def test_two_services_backing_it_report_the_widest_view_not_the_sum(self):
        """A headless Service beside a ClusterIP one would otherwise report
        twice as many endpoints as the workload has replicas."""
        wl = self.wl(tsc=self.ADVISORY)
        ctx = self.ctx(wl, services=[("api-svc", [self.NODE_A, self.NODE_A]), ("api-headless", [self.NODE_A, self.NODE_A])])
        hit = collect.check_spread_not_achieved(wl, ctx)
        self.assertIn("all 2 ready endpoints", hit["excerpt"])

    def test_a_daemonset_is_never_flagged(self):
        wl = self.wl(tsc=self.ADVISORY, kind="DaemonSet")
        self.assertIsNone(collect.check_spread_not_achieved(wl, self.ctx(wl)))

    def test_single_replica_is_never_flagged(self):
        wl = self.wl(replicas=1, tsc=self.ADVISORY)
        self.assertIsNone(collect.check_spread_not_achieved(wl, self.ctx(wl)))


class TestEndpointNodeCollection(unittest.TestCase):
    """The dump-level half of §3.19: which nodes the slices name."""

    def dump(self, **kwargs):
        return dump_of(
            service("api-svc", selector={"app": "api"}),
            endpointslice("api-svc", **kwargs),
        )

    def nodes_for(self, **kwargs):
        entries = collect.services_with_endpoints(self.dump(**kwargs))
        return entries[0]["endpoint_nodes"]

    def test_ready_endpoints_report_their_node(self):
        self.assertEqual(self.nodes_for(addresses=2, nodes=["n1", "n2"]), ["n1", "n2"])

    def test_an_endpoint_marked_not_ready_is_excluded(self):
        self.assertEqual(self.nodes_for(addresses=2, nodes=["n1", "n2"], unready=(1,)), ["n1"])

    def test_a_terminating_endpoint_is_excluded(self):
        # The terminating endpoints name a node of their own, so only the
        # terminating guard -- not the missing-nodeName one -- keeps them out.
        dump = self.dump(addresses=1, terminating=2, nodes=["n1"])
        for item in dump["items"]:
            for endpoint in item.get("endpoints") or []:
                if (endpoint.get("conditions") or {}).get("terminating"):
                    endpoint["nodeName"] = "n9"
        self.assertEqual(collect.services_with_endpoints(dump)[0]["endpoint_nodes"], ["n1"])

    def test_an_endpoint_with_no_node_name_is_skipped_not_counted_as_a_node(self):
        """Counting it as an unknown node would read as spread."""
        self.assertEqual(self.nodes_for(addresses=2, nodes=["n1"]), ["n1"])

    def test_nodes_named_by_endpoints_spans_system_namespaces(self):
        """The question is how many nodes the cluster has, and kube-system's
        slices are usually the widest sample available here."""
        dump = dump_of(
            service("api-svc", selector={"app": "api"}),
            endpointslice("api-svc", addresses=1, nodes=["n1"]),
            endpointslice("kube-dns", ns="kube-system", addresses=2, nodes=["n1", "n2"]),
        )
        self.assertEqual(collect.nodes_named_by_endpoints(dump), {"n1", "n2"})

    def test_a_slice_naming_no_nodes_contributes_nothing(self):
        self.assertEqual(collect.nodes_named_by_endpoints(self.dump(addresses=2)), set())

    def test_zones_named_by_endpoints_reads_the_slices_own_zone_field(self):
        """What tells a zonal cluster from a regional one.

        The remediation turns on it: a `DoNotSchedule` rule keyed on a zone
        every node already shares leaves the skew at nought and moves no pod,
        so the pull request merges and the finding survives.
        """
        dump = dump_of(
            service("api-svc", selector={"app": "api"}),
            endpointslice("api-svc", addresses=2, nodes=["n1", "n2"], zones=["us-east4-a", "us-east4-a"]),
            endpointslice("kube-dns", ns="kube-system", addresses=1, nodes=["n3"], zones=["us-east4-b"]),
        )
        self.assertEqual(collect.zones_named_by_endpoints(dump), {"us-east4-a", "us-east4-b"})

    def test_a_slice_with_no_zone_field_contributes_nothing(self):
        # The field is optional, so an empty set means "the dump does not say",
        # not "one zone" -- which is why the excerpt drops the clause instead
        # of writing a count.
        self.assertEqual(collect.zones_named_by_endpoints(self.dump(addresses=2, nodes=["n1", "n2"])), set())


class TestProbes(unittest.TestCase):
    def wl(self, probes=None):
        d = deployment("api")
        d["spec"]["template"]["metadata"] = {"labels": {"app": "api"}}
        if probes is not None:
            d["spec"]["template"]["spec"]["containers"][0].update(probes)
        return collect.normalize_workloads(dump_of(d))[0]

    def svc_ctx(self):
        return context_of(services={"default": [service("s", selector={"app": "api"})]})

    def test_readiness_missing_on_a_service_backed_workload_is_flagged(self):
        self.assertIsNotNone(collect.check_probes_readiness(self.wl(), self.svc_ctx()))

    def test_readiness_present_is_not_flagged(self):
        hit = collect.check_probes_readiness(
            self.wl(probes={"readinessProbe": {"httpGet": {"path": "/", "port": 80}}}), self.svc_ctx()
        )
        self.assertIsNone(hit)

    def test_readiness_is_never_flagged_with_no_service(self):
        self.assertIsNone(collect.check_probes_readiness(self.wl(), context_of()))

    def test_an_external_name_service_does_not_count_as_backing(self):
        ctx = context_of(services={"default": [service("s", svc_type="ExternalName")]})
        self.assertIsNone(collect.check_probes_readiness(self.wl(), ctx))

    def test_a_self_health_sidecar_is_never_flagged(self):
        d = deployment("api")
        d["spec"]["template"]["metadata"] = {"labels": {"app": "api"}}
        d["spec"]["template"]["spec"]["containers"].append({"name": "istio-proxy", "resources": {}})
        workload = collect.normalize_workloads(dump_of(d))[0]
        hit = collect.check_probes_readiness(workload, self.svc_ctx())
        self.assertNotIn("istio-proxy", hit["excerpt"] if hit else "")

    def gateway_shaped(self, sidecar_readiness=True):
        """The live `platform-agent-gateway` shape, which this check got wrong.

        Three containers behind one Service: the app on 8642 with a probe, a
        probe-less log shipper serving nothing, and a native sidecar holding
        8643 -- the port the Service actually targets.
        """
        d = deployment("api")
        d["spec"]["template"]["metadata"] = {"labels": {"app": "api"}}
        d["spec"]["template"]["spec"]["containers"] = [
            {
                "name": "platform-agent",
                "ports": [{"containerPort": 8642, "name": "api"}],
                "readinessProbe": {"exec": {"command": ["true"]}},
            },
            {"name": "fluent-bit"},
        ]
        sidecar = {
            "name": "envoy-credential-proxy",
            "restartPolicy": "Always",
            "ports": [{"containerPort": 8765, "name": "cred-proxy"}, {"containerPort": 8643, "name": "proxy-api"}],
        }
        if sidecar_readiness:
            sidecar["readinessProbe"] = {"exec": {"command": ["true"]}}
        d["spec"]["template"]["spec"]["initContainers"] = [
            {"name": "sandbox-credential-cleanup"},
            sidecar,
        ]
        ctx = context_of(
            services={
                "default": [service("s", selector={"app": "api"}, ports=[{"port": 8642, "targetPort": 8643}])]
            }
        )
        return collect.normalize_workloads(dump_of(d))[0], ctx

    def test_a_probe_on_the_native_sidecar_holding_the_service_port_counts(self):
        """The container serving `targetPort` may be an `initContainer`.

        `initContainers` with `restartPolicy: Always` are native sidecars: they
        run for the pod's whole life and serve ports like anything else. This
        check read `containers` only, so on the gateway it saw 8643 served by
        nobody, judged the probe-less `fluent-bit` to be the workload's answer
        for readiness, and reported a Service-backed workload with no readiness
        probe -- while both the app container and the container actually behind
        the Service port had one. A false positive stated as fact about a live
        deployment, which is the kind that costs a reader the most to disprove.
        """
        workload, ctx = self.gateway_shaped()
        self.assertIsNone(collect.check_probes_readiness(workload, ctx))

    def test_the_container_behind_the_service_port_is_still_required_to_probe(self):
        """The narrowing must not amount to switching the check off.

        Same three containers, same Service; the only change is that the
        sidecar holding 8643 has no readiness probe. Traffic now reaches a
        container with no readiness signal, which is exactly what this check is
        for -- and the app container's probe two lines up must not excuse it.
        """
        workload, ctx = self.gateway_shaped(sidecar_readiness=False)
        hit = collect.check_probes_readiness(workload, ctx)
        self.assertIsNotNone(hit)
        self.assertIn("envoy-credential-proxy", hit["excerpt"])
        # And only that one: naming `fluent-bit` here is what sent a reader
        # looking at the wrong container in the first place.
        self.assertNotIn("fluent-bit", hit["excerpt"])

    def test_a_pod_that_declares_no_ports_keeps_every_container_in_the_path(self):
        """Declaring `ports` is optional, so absence is not evidence.

        kubelet routes to a `targetPort` no container ever named. With nothing
        to match on there is no routing to infer, and narrowing to the empty
        set would silently retire the check for every workload that omits the
        field -- a far bigger hole than the false positive being fixed.
        """
        d = deployment("api")
        d["spec"]["template"]["metadata"] = {"labels": {"app": "api"}}
        ctx = context_of(
            services={"default": [service("s", selector={"app": "api"}, ports=[{"port": 80, "targetPort": 8080}])]}
        )
        workload = collect.normalize_workloads(dump_of(d))[0]
        hit = collect.check_probes_readiness(workload, ctx)
        self.assertIsNotNone(hit)
        self.assertIn("app", hit["excerpt"])

    def metrics_ctx(self, ports):
        return context_of(services={"default": [service("s-metrics", selector={"app": "api"}, ports=ports)]})

    def test_a_metrics_only_service_is_named_as_such(self):
        """3.9's Impact line claims production traffic; say when there is none.

        Live case, 2026-09-01: three of the six findings this check published
        were on workloads whose only Service exposes one scrape port --
        `cert-manager` and `cert-manager-cainjector` on `http-metrics/9402`,
        `argocd-notifications-controller` on `metrics/9001`. Each shipped
        "Every rollout sends production traffic to pods that are not yet
        serving", which is not true of any of them.
        """
        hit = collect.check_probes_readiness(self.wl(), self.metrics_ctx([{"name": "http-metrics", "port": 9402}]))
        self.assertIsNotNone(hit)
        self.assertIn("s-metrics[http-metrics]", hit["excerpt"])
        self.assertIn("(metrics scrape only)", hit["excerpt"])

    def test_a_serving_port_alongside_a_metrics_port_is_still_serving(self):
        # `argocd-applicationset-controller`: `webhook/7000` and
        # `metrics/8080`. One real port is enough to make the traffic claim
        # true, so the suppression must not trigger on "contains a metrics
        # port".
        hit = collect.check_probes_readiness(
            self.wl(), self.metrics_ctx([{"name": "webhook", "port": 7000}, {"name": "metrics", "port": 8080}])
        )
        self.assertIn("(serving traffic)", hit["excerpt"])

    def test_a_second_service_that_serves_traffic_defeats_the_suppression(self):
        # `argocd-server` has both `argocd-server` (http/https) and
        # `argocd-server-metrics`. Judging the Services one at a time would
        # call the workload metrics-only on the strength of the wrong one.
        ctx = context_of(
            services={
                "default": [
                    service("s-metrics", selector={"app": "api"}, ports=[{"name": "metrics", "port": 8083}]),
                    service("s", selector={"app": "api"}, ports=[{"name": "http", "port": 80}]),
                ]
            }
        )
        hit = collect.check_probes_readiness(self.wl(), ctx)
        self.assertIn("(serving traffic)", hit["excerpt"])

    def test_an_unnamed_port_is_not_assumed_to_be_metrics(self):
        # Suppressing the impact claim wrongly is the expensive error, so an
        # unnamed port -- common on a single-port Service -- keeps the finding
        # reading exactly as it did before.
        hit = collect.check_probes_readiness(self.wl(), self.metrics_ctx([{"port": 9402}]))
        self.assertIn("(serving traffic)", hit["excerpt"])

    def test_a_service_with_no_ports_at_all_is_neither_scope(self):
        # Not metrics-only -- nothing says these pods are scraped. Not serving
        # either: a Service declaring no ports routes nothing through its own
        # ClusterIP, so "every rollout sends production traffic to pods that
        # are not yet serving" is a claim about traffic that does not exist.
        hit = collect.check_probes_readiness(self.wl(), self.metrics_ctx([]))
        self.assertIn("(no ports declared)", hit["excerpt"])
        self.assertIn("s-metrics[no ports]", hit["excerpt"])

    def test_liveness_missing_is_flagged_with_no_service_required(self):
        self.assertIsNotNone(collect.check_probes_liveness(self.wl(), context_of()))

    def test_liveness_present_is_not_flagged(self):
        hit = collect.check_probes_liveness(
            self.wl(probes={"livenessProbe": {"httpGet": {"path": "/", "port": 80}}}), context_of()
        )
        self.assertIsNone(hit)

    def test_liveness_and_readiness_are_reported_separately_never_merged(self):
        # A workload missing both must be two findings under two checks, not
        # one -- they carry different severities and impacts.
        readiness_hit = collect.check_probes_readiness(self.wl(), self.svc_ctx())
        liveness_hit = collect.check_probes_liveness(self.wl(), context_of())
        self.assertIsNotNone(readiness_hit)
        self.assertIsNotNone(liveness_hit)


class TestReadinessDerivedFromLiveness(unittest.TestCase):
    """§3.9's one exception to "do not generate probe YAML".

    A container declaring a liveness probe has already told the kubelet which
    handler answers for it, so a readiness probe copied from it asks nothing
    new of the workload. The deadline is the part that has to be got right:
    equal deadlines are §3.18, so a fix that produced them would close this
    finding by opening that one on the same container.
    """

    MARKER = "readiness derivable from the liveness probe:"

    def wl(self, *containers):
        dep = deployment("api")
        dep["spec"]["template"]["metadata"] = {"labels": {"app": "api"}}
        dep["spec"]["template"]["spec"]["containers"] = list(containers)
        return collect.normalize_workloads(dump_of(dep))[0]

    def svc_ctx(self):
        return context_of(services={"default": [service("s", selector={"app": "api"})]})

    def container(self, name="app", liveness=None):
        c = {"name": name, "resources": {}}
        if liveness is not None:
            c["livenessProbe"] = liveness
        return c

    def excerpt(self, *containers):
        hit = collect.check_probes_readiness(self.wl(*containers), self.svc_ctx())
        self.assertIsNotNone(hit)
        return hit["excerpt"]

    def test_a_liveness_probe_supplies_the_handler_and_a_tighter_deadline(self):
        # Defaults put liveness at 0 + 3 x 10 = 30s, so readiness gets 5s
        # periods and a threshold of 3 -- 15s, half of it.
        line = self.excerpt(self.container(liveness={"httpGet": {"path": "/livez", "port": 9403}}))
        self.assertIn(self.MARKER, line)
        self.assertIn('httpGet {"path": "/livez", "port": 9403}', line)
        self.assertIn("initialDelaySeconds 0, periodSeconds 5, failureThreshold 3", line)
        self.assertIn("~15s, inside liveness's ~30s", line)

    def test_the_derived_deadline_is_always_strictly_inside_the_liveness_one(self):
        for probe, expected in (
            ({"httpGet": {"port": 1}, "failureThreshold": 2, "periodSeconds": 10}, (5, 2)),
            ({"httpGet": {"port": 1}, "periodSeconds": 30}, (5, 9)),
            ({"httpGet": {"port": 1}, "failureThreshold": 2, "periodSeconds": 3}, (5, 1)),
            ({"httpGet": {"port": 1}, "failureThreshold": 1, "periodSeconds": 5}, (1, 1)),
            ({"httpGet": {"port": 1}, "initialDelaySeconds": 60}, (5, 9)),
        ):
            with self.subTest(probe=probe):
                live_at = collect._probe_deadline_seconds(probe)
                period, threshold = expected
                self.assertLess(period * threshold, live_at)
                self.assertIn(
                    f"periodSeconds {period}, failureThreshold {threshold}",
                    self.excerpt(self.container(liveness=probe)),
                )

    def test_a_liveness_deadline_with_no_room_beneath_it_stays_manual(self):
        # `failureThreshold: 1, periodSeconds: 1` fails at ~1s. Nothing fits
        # inside that, and inventing a readiness probe that gives up sooner
        # than the kubelet can call it twice is worse than the finding.
        probe = {"httpGet": {"port": 1}, "failureThreshold": 1, "periodSeconds": 1}
        self.assertEqual(collect._probe_deadline_seconds(probe), 1)
        self.assertNotIn(self.MARKER, self.excerpt(self.container(liveness=probe)))

    def test_a_liveness_probe_with_no_recognised_handler_supplies_nothing(self):
        self.assertNotIn(self.MARKER, self.excerpt(self.container(liveness={"initialDelaySeconds": 5})))

    def test_no_liveness_probe_leaves_the_finding_manual(self):
        self.assertNotIn(self.MARKER, self.excerpt(self.container()))

    def test_one_container_short_of_a_handler_withholds_the_line_from_all_of_them(self):
        # All or nothing: a patch giving readiness to `app` and not to `web`
        # leaves this finding open on `web`, and a finding its own remediation
        # does not close is one the audit republishes every day.
        line = self.excerpt(
            self.container(name="app", liveness={"httpGet": {"path": "/livez", "port": 9403}}),
            self.container(name="web"),
        )
        self.assertIn("app, web", line)
        self.assertNotIn(self.MARKER, line)

    def test_every_container_with_a_handler_is_named_in_the_line(self):
        line = self.excerpt(
            self.container(name="app", liveness={"httpGet": {"path": "/livez", "port": 9403}}),
            self.container(name="web", liveness={"tcpSocket": {"port": 8080}}),
        )
        self.assertIn("app: copy the liveness handler", line)
        self.assertIn('web: copy the liveness handler tcpSocket {"port": 8080}', line)

    def test_a_container_that_already_has_readiness_is_not_in_the_line(self):
        line = self.excerpt(
            self.container(name="app", liveness={"httpGet": {"path": "/livez", "port": 9403}}),
            {
                "name": "web",
                "resources": {},
                "livenessProbe": {"tcpSocket": {"port": 8080}},
                "readinessProbe": {"tcpSocket": {"port": 8080}},
            },
        )
        self.assertNotIn("web", line.split(self.MARKER)[-1])


class TestLivenessPreemptsReadiness(unittest.TestCase):
    HTTP = {"path": "/healthz", "port": 8080}

    def wl(self, *containers):
        dep = deployment("api")
        dep["spec"]["template"]["spec"]["containers"] = list(containers)
        return collect.normalize_workloads(dump_of(dep))[0]

    def container(self, name="app", liveness=..., readiness=..., **kwargs):
        """A container whose two probes share `HTTP` unless overridden.

        The default is the finding: one handler copied into both blocks with
        every timing left at its default, which makes the two deadlines equal.
        """
        c = {"name": name, "resources": {}, **kwargs}
        if liveness is not ...:
            c["livenessProbe"] = liveness
        else:
            c["livenessProbe"] = {"httpGet": dict(self.HTTP)}
        if readiness is not ...:
            c["readinessProbe"] = readiness
        else:
            c["readinessProbe"] = {"httpGet": dict(self.HTTP)}
        return c

    def hits(self, *containers):
        return collect.check_liveness_preempts_readiness(self.wl(*containers), context_of())

    def test_two_defaulted_probes_on_one_handler_are_flagged(self):
        self.assertIsNotNone(self.hits(self.container()))

    def test_headroom_on_the_liveness_side_is_the_documented_shape(self):
        # The majority of shared-handler pairs, and not a finding: readiness
        # gives up at 30s and liveness at 300s, so a blip de-registers the pod
        # and only a wedged process is ever restarted.
        self.assertIsNone(
            self.hits(
                self.container(
                    liveness={"httpGet": dict(self.HTTP), "failureThreshold": 30},
                )
            )
        )

    def test_one_second_of_headroom_is_still_headroom(self):
        # The rule is `<=`, not "close enough". A deliberate margin the
        # collector second-guesses is a finding its author cannot act on.
        self.assertIsNone(
            self.hits(
                self.container(
                    liveness={"httpGet": dict(self.HTTP), "initialDelaySeconds": 1},
                )
            )
        )

    def test_liveness_strictly_sooner_is_flagged(self):
        self.assertIsNotNone(
            self.hits(
                self.container(
                    liveness={"httpGet": dict(self.HTTP), "failureThreshold": 1},
                )
            )
        )

    def test_different_handlers_are_never_flagged(self):
        # Same failure on paper, but a local liveness endpoint beside a
        # dependency-aware readiness one is the correct shape and reads
        # identically from here.
        self.assertIsNone(
            self.hits(
                self.container(
                    liveness={"httpGet": {"path": "/livez", "port": 8080}, "failureThreshold": 1},
                )
            )
        )

    def test_a_different_port_on_the_same_path_is_a_different_handler(self):
        self.assertIsNone(
            self.hits(self.container(liveness={"httpGet": {"path": "/healthz", "port": 9090}}))
        )

    def test_field_order_within_a_handler_does_not_decide_it(self):
        # `json.dumps(sort_keys=True)`'s job. The apiserver does not reorder,
        # but two hands writing the same probe can.
        self.assertIsNotNone(
            self.hits(
                self.container(
                    liveness={"httpGet": {"port": 8080, "path": "/healthz"}},
                    readiness={"httpGet": {"path": "/healthz", "port": 8080}},
                )
            )
        )

    def test_each_handler_kind_is_recognised(self):
        for kind, body in (
            ("tcpSocket", {"port": 8080}),
            ("exec", {"command": ["/bin/health"]}),
            ("grpc", {"port": 8080}),
        ):
            with self.subTest(kind=kind):
                probe = {kind: dict(body)}
                hit = self.hits(self.container(liveness=dict(probe), readiness=dict(probe)))
                self.assertIsNotNone(hit)
                self.assertIn(kind, hit["excerpt"])

    def test_a_probe_with_no_handler_this_knows_is_not_flagged(self):
        # The exemption `_PROBE_HANDLERS` has to stay exhaustive to avoid: a
        # handler added to `v1.Probe` later reads as "no handler" here, and
        # silently exempts the container rather than failing loudly.
        probe = {"someFutureHandler": {"port": 8080}}
        self.assertIsNone(self.hits(self.container(liveness=dict(probe), readiness=dict(probe))))

    def test_a_container_with_only_one_probe_belongs_to_3_9_or_3_10(self):
        self.assertIsNone(self.hits(self.container(liveness=None)))
        self.assertIsNone(self.hits(self.container(readiness=None)))

    def test_neither_probe_declared_is_not_this_finding(self):
        self.assertIsNone(self.hits(self.container(liveness=None, readiness=None)))

    def test_a_self_health_sidecar_is_skipped(self):
        self.assertIsNone(self.hits(self.container(name="istio-proxy")))

    def test_a_flagged_container_beside_a_skipped_one_still_reports(self):
        hit = self.hits(self.container(name="istio-proxy"), self.container(name="app"))
        self.assertIsNotNone(hit)
        self.assertNotIn("istio-proxy", hit["excerpt"])

    def test_every_offending_container_is_named(self):
        hit = self.hits(self.container(name="app"), self.container(name="sidecar"))
        self.assertIn("app", hit["excerpt"])
        self.assertIn("sidecar", hit["excerpt"])

    def test_the_excerpt_carries_both_deadlines_and_the_shared_handler(self):
        # The remediation is a choice between widening, repointing and
        # deleting, and none of those is decidable without seeing how much
        # room there is between the two numbers.
        hit = self.hits(
            self.container(liveness={"httpGet": dict(self.HTTP), "failureThreshold": 2})
        )
        self.assertIn("~20s", hit["excerpt"])
        self.assertIn("~30s", hit["excerpt"])
        self.assertIn("/healthz", hit["excerpt"])

    def test_the_deadlines_are_written_as_approximations(self):
        # They are `initialDelay + failureThreshold x period`, which is not
        # wall-clock truth for either probe. Publishing a bare number invites
        # a reader to check it against a stopwatch and conclude the check is
        # broken.
        self.assertIn("~", self.hits(self.container())["excerpt"])

    def test_initial_delay_counts_towards_the_deadline(self):
        # A liveness probe that merely starts later is not safer once it does:
        # 60 + 3x10 beats readiness's 30, so it is not flagged...
        self.assertIsNone(
            self.hits(
                self.container(liveness={"httpGet": dict(self.HTTP), "initialDelaySeconds": 60})
            )
        )
        # ...but the same delay on the readiness side puts liveness first again.
        self.assertIsNotNone(
            self.hits(
                self.container(readiness={"httpGet": dict(self.HTTP), "initialDelaySeconds": 60})
            )
        )

    def test_period_and_threshold_both_move_the_deadline(self):
        self.assertIsNone(
            self.hits(self.container(liveness={"httpGet": dict(self.HTTP), "periodSeconds": 60}))
        )

    def test_timeout_and_success_threshold_do_not_move_it(self):
        # Neither changes when a probe gives up, so neither can turn a finding
        # into a pass. `successThreshold` is readiness-only in the API and
        # `timeoutSeconds` bounds one attempt, not the sequence.
        self.assertIsNotNone(
            self.hits(
                self.container(
                    liveness={"httpGet": dict(self.HTTP), "timeoutSeconds": 30},
                    readiness={"httpGet": dict(self.HTTP), "successThreshold": 5},
                )
            )
        )

    def test_the_object_is_named_kind_slash_name(self):
        self.assertEqual(self.hits(self.container())["object"], "Deployment/api")


class TestSingleReplica(unittest.TestCase):
    def wl(self, replicas=1, strategy=None, kind="Deployment", volumes=None):
        d = deployment("api", **{"spec.replicas": replicas})
        d["kind"] = kind
        d["spec"]["template"]["metadata"] = {"labels": {"app": "api"}}
        if strategy is not None:
            d["spec"]["strategy"] = {"type": strategy}
        if volumes is not None:
            d["spec"]["template"]["spec"]["volumes"] = list(volumes)
        return collect.normalize_workloads(dump_of(d))[0]

    def svc_ctx(self, ports=None, claims=()):
        return context_of(
            services={"default": [service("s", selector={"app": "api"}, **({"ports": ports} if ports else {}))]},
            claims=collect.claims_by_key(dump_of(*claims)),
        )

    def test_a_single_replica_service_backed_deployment_is_flagged(self):
        self.assertIsNotNone(collect.check_single_replica(self.wl(), self.svc_ctx()))

    def test_a_metrics_only_service_is_named_as_such(self):
        """The same claim readiness makes, so the same qualifier.

        Live case, 2026-09-01: this check published `cert-manager` and
        `cert-manager-cainjector` as "single replica, Service-backed" -- 3.8's
        Impact line is that a rollout drops user traffic -- in the same report
        where the readiness check had already said the only Service in front of
        each is `http-metrics/9402`. Giving one check the exposure line and not
        the other is how the report contradicted itself.
        """
        hit = collect.check_single_replica(self.wl(), self.svc_ctx([{"name": "http-metrics", "port": 9402}]))
        self.assertIsNotNone(hit)
        self.assertIn("s[http-metrics]", hit["excerpt"])
        self.assertIn("(metrics scrape only)", hit["excerpt"])

    def test_a_serving_port_makes_the_traffic_claim_true(self):
        hit = collect.check_single_replica(self.wl(), self.svc_ctx([{"name": "http", "port": 80}]))
        self.assertIn("(serving traffic)", hit["excerpt"])

    def test_an_unnamed_port_is_not_assumed_to_be_metrics(self):
        # `kube-agents-webhook-service` is an unnamed 443 to an admission
        # webhook -- traffic, and the finding has to keep saying so.
        hit = collect.check_single_replica(self.wl(), self.svc_ctx([{"port": 443, "targetPort": 10250}]))
        self.assertIn("(serving traffic)", hit["excerpt"])

    def test_a_service_with_no_ports_at_all_is_neither_scope(self):
        # The readiness sibling's reason, and the same trap: 3.11's Impact line
        # is a full outage for "this service", which a Service exposing no port
        # cannot have.
        hit = collect.check_single_replica(self.wl(), self.svc_ctx())
        self.assertIn("(no ports declared)", hit["excerpt"])

    def test_multi_replica_is_never_flagged(self):
        self.assertIsNone(collect.check_single_replica(self.wl(replicas=2), self.svc_ctx()))

    def test_a_statefulset_is_never_flagged(self):
        self.assertIsNone(collect.check_single_replica(self.wl(kind="StatefulSet"), self.svc_ctx()))

    def test_recreate_strategy_is_never_flagged(self):
        self.assertIsNone(collect.check_single_replica(self.wl(strategy="Recreate"), self.svc_ctx()))

    def test_no_service_means_never_flagged(self):
        self.assertIsNone(collect.check_single_replica(self.wl(), context_of()))

    def test_an_exclusive_claim_is_named_so_the_manifest_arm_can_refuse(self):
        """§3.11's manifest arm sets the replica key to 2; here that deadlocks.

        The second pod cannot schedule off the first one's node and waits in
        `ContainerCreating` with a `Multi-Attach error` indefinitely. The
        finding still stands -- a drain is a full outage whatever the volume
        does -- so the bar has to travel in the excerpt, which is the only
        field `emit` carries through and `adopt_collector_evidence` publishes
        verbatim.
        """
        volumes = [{"name": "data", "persistentVolumeClaim": {"claimName": "api-data"}}]
        hit = collect.check_single_replica(
            self.wl(volumes=volumes), self.svc_ctx(claims=[pvc("api-data")])
        )
        self.assertIn("mounts an exclusively-attachable claim:", hit["excerpt"])
        self.assertIn("api-data", hit["excerpt"])
        self.assertIn("ReadWriteOnce", hit["excerpt"])

    def test_a_shareable_claim_does_not_bar_the_manifest_arm(self):
        volumes = [{"name": "data", "persistentVolumeClaim": {"claimName": "api-data"}}]
        hit = collect.check_single_replica(
            self.wl(volumes=volumes),
            self.svc_ctx(claims=[pvc("api-data", modes=("ReadWriteMany",))]),
        )
        self.assertNotIn("exclusively-attachable", hit["excerpt"])


def cronjob(
    name="cj",
    ns="default",
    schedule="0 * * * *",
    suspend=None,
    last_schedule="2026-09-06T21:00:00Z",
    last_success="2026-08-27T18:18:39Z",
    created="2026-08-01T00:00:00Z",
    labels=None,
    annotations=None,
):
    doc = {
        "kind": "CronJob",
        "metadata": {
            "namespace": ns,
            "name": name,
            "creationTimestamp": created,
            "labels": labels or {},
            "annotations": annotations or {},
        },
        "spec": {"schedule": schedule},
        "status": {},
    }
    if suspend is not None:
        doc["spec"]["suspend"] = suspend
    if last_schedule is not None:
        doc["status"]["lastScheduleTime"] = last_schedule
    if last_success is not None:
        doc["status"]["lastSuccessfulTime"] = last_success
    return doc


def job(
    name,
    ns="default",
    owner="cj",
    succeeded=None,
    failed=None,
    created="2026-09-06T20:00:00Z",
    reason=None,
    started=None,
    completed=None,
):
    status = {}
    if succeeded is not None:
        status["succeeded"] = succeeded
    if failed is not None:
        status["failed"] = failed
    if not status:
        # Neither field set is what an in-flight Job looks like.
        status["active"] = 1
    if reason is not None:
        status["conditions"] = [{"type": "Failed", "status": "True", "reason": reason}]
    elif failed and not succeeded and "active" not in status:
        # A failed Job is finished only once its terminal condition is set;
        # the fixture's `failed=` means that one unless `reason=` says why.
        status["conditions"] = [{"type": "Failed", "status": "True"}]
    # 3.15 measures a run from these two; 3.12 reads neither. Defaulting them
    # to absent keeps every 3.12 fixture above saying exactly what it said.
    if started is not None:
        status["startTime"] = started
    if completed is not None:
        status["completionTime"] = completed
    return {
        "kind": "Job",
        "metadata": {
            "namespace": ns,
            "name": name,
            "creationTimestamp": created,
            "ownerReferences": ([{"kind": "CronJob", "name": owner}] if owner else []),
        },
        "status": status,
    }


class TestScheduleNeverSucceeds(unittest.TestCase):
    """§3.12. The live case is `kube-agents-selfimprove` on kube-agents-host.

    Hourly, not suspended, three retained Jobs all failed, and a
    `lastSuccessfulTime` ten days behind its `lastScheduleTime`. It ships with
    every kube-agents install, and before this check nothing in 94 of them
    looked at a CronJob at all -- obtainability's S5 drops Jobs and CronJobs
    from the workload set on purpose, and cost's `terminal-pods` excludes Jobs
    owned by a CronJob by name.
    """

    def hits(self, *items):
        return collect.check_schedule_never_succeeds(context_of(dump_of(*items)))

    def test_the_live_hub_case_is_flagged(self):
        hits = self.hits(
            cronjob("kube-agents-selfimprove", ns="kubeagents-system"),
            job("s-1", ns="kubeagents-system", owner="kube-agents-selfimprove", failed=1),
            job("s-2", ns="kubeagents-system", owner="kube-agents-selfimprove", failed=1),
            job("s-3", ns="kubeagents-system", owner="kube-agents-selfimprove", failed=1),
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "CronJob/kube-agents-selfimprove")
        self.assertEqual(hits[0]["namespace"], "kubeagents-system")

    def test_the_excerpt_carries_the_outage_length_and_the_retained_count(self):
        # A `manual` remediation is prose the owner has to act on, so the
        # excerpt is the whole finding: how long it has been down, and enough
        # of the schedule to tell an hourly outage from an annual one.
        hits = self.hits(cronjob(), job("j1", failed=1))
        self.assertIn("'0 * * * *'", hits[0]["excerpt"])
        self.assertIn("243h without a successful run", hits[0]["excerpt"])
        self.assertIn("lastSuccessfulTime=2026-08-27T18:18:39Z", hits[0]["excerpt"])
        self.assertIn("1 retained Job(s), all failed", hits[0]["excerpt"])

    def test_the_excerpt_names_the_newest_jobs_failure_reason(self):
        # BackoffLimitExceeded and DeadlineExceeded take different fixes, and
        # the Job already says which one applies -- so the finding does too,
        # rather than sending the reader to `kubectl describe` for it.
        hits = self.hits(
            cronjob(),
            job("j1", failed=1, created="2026-09-06T19:00:00Z", reason="DeadlineExceeded"),
            job("j2", failed=1, created="2026-09-06T20:00:00Z", reason="BackoffLimitExceeded"),
        )
        self.assertIn("(most recent: BackoffLimitExceeded)", hits[0]["excerpt"])
        self.assertNotIn("DeadlineExceeded", hits[0]["excerpt"])

    def test_a_failed_job_with_no_conditions_adds_no_clause(self):
        # Nothing to say is said as nothing, not as an empty parenthesis.
        excerpt = self.hits(cronjob(), job("j1", failed=1))[0]["excerpt"]
        self.assertIn("1 retained Job(s), all failed", excerpt)
        self.assertNotIn("most recent", excerpt)

    def test_a_blank_reason_adds_no_clause(self):
        blank = job("j1", failed=1)
        blank["status"]["conditions"] = [{"type": "Failed", "status": "True", "reason": ""}]
        self.assertNotIn("most recent", self.hits(cronjob(), blank)[0]["excerpt"])

    def test_a_condition_that_is_not_a_true_failed_is_ignored(self):
        # `Failed` with status False, and `SuccessCriteriaMet`, are both things
        # a Job carries; neither is the reason this Job failed.
        noisy = job("j1", failed=1)
        noisy["status"]["conditions"] = [
            {"type": "Failed", "status": "False", "reason": "NotThisOne"},
            {"type": "SuccessCriteriaMet", "status": "True", "reason": "NorThis"},
            {"type": "Failed", "status": "True"},
        ]
        excerpt = self.hits(cronjob(), noisy)[0]["excerpt"]
        self.assertNotIn("NotThisOne", excerpt)
        self.assertNotIn("NorThis", excerpt)

    def test_a_suspended_cronjob_is_never_flagged(self):
        self.assertEqual(self.hits(cronjob(suspend=True), job("j1", failed=1)), [])

    def test_a_cronjob_that_has_never_fired_is_never_flagged(self):
        # No `lastScheduleTime` and it has either not come due yet or the
        # scheduler is not firing it -- a different fault, and one this check
        # would misattribute to the workload.
        self.assertEqual(self.hits(cronjob(last_schedule=None), job("j1", failed=1)), [])

    def test_a_success_anywhere_in_the_retained_history_silences_it(self):
        # What separates "failing" from "failed once". The gap is 243h and two
        # of the three Jobs failed, and it still says nothing.
        self.assertEqual(
            self.hits(
                cronjob(),
                job("j1", failed=1),
                job("j2", succeeded=1),
                job("j3", failed=1),
            ),
            [],
        )

    def test_a_gap_under_the_threshold_is_not_yet_chronic(self):
        self.assertEqual(
            self.hits(
                cronjob(last_schedule="2026-09-06T21:00:00Z", last_success="2026-09-06T02:00:00Z"),
                job("j1", failed=1),
            ),
            [],
        )

    def test_the_threshold_is_a_floor_not_a_window(self):
        # Exactly STALE_SUCCESS_GAP_HOURS fires; a minute under does not.
        self.assertEqual(
            len(self.hits(cronjob(last_success="2026-09-05T21:00:00Z"), job("j1", failed=1))), 1
        )
        self.assertEqual(
            self.hits(cronjob(last_success="2026-09-05T21:01:00Z"), job("j1", failed=1)), []
        )

    def test_a_cronjob_that_has_never_succeeded_measures_from_its_creation(self):
        # The worst case in the check's remit, and the one arm an implementation
        # keyed on `lastSuccessfulTime` alone stays silent about.
        hits = self.hits(cronjob(last_success=None, created="2026-08-01T00:00:00Z"), job("j1", failed=1))
        self.assertEqual(len(hits), 1)
        self.assertIn("creationTimestamp=2026-08-01T00:00:00Z", hits[0]["excerpt"])

    def test_a_never_succeeded_cronjob_created_an_hour_ago_is_not_yet_chronic(self):
        self.assertEqual(
            self.hits(cronjob(last_success=None, created="2026-09-06T20:00:00Z"), job("j1", failed=1)), []
        )

    def test_a_history_of_only_active_jobs_says_nothing(self):
        # Nothing has finished, so nothing has failed.
        self.assertEqual(self.hits(cronjob(), job("j1")), [])

    def test_an_in_flight_job_does_not_veto_a_failing_history(self):
        # An hourly schedule almost always has one in flight. Letting it count
        # would make the check unreachable on exactly the schedules that fail
        # most often.
        self.assertEqual(len(self.hits(cronjob(), job("j1", failed=1), job("j2"))), 1)

    def test_a_cronjob_retaining_no_jobs_at_all_says_nothing(self):
        # `failedJobsHistoryLimit: 0` is legal, and it leaves this check with no
        # evidence rather than with evidence of failure.
        self.assertEqual(self.hits(cronjob()), [])

    def test_the_opt_out_label_silences_it(self):
        self.assertEqual(
            self.hits(cronjob(labels={collect.OPT_OUT_KEY: "exempt"}), job("j1", failed=1)), []
        )

    def test_the_opt_out_annotation_silences_it(self):
        # S4 is the only way an operator who knows the CronJob is broken can
        # make this finding stop -- every other check in this audit reports a
        # shape they can just fix.
        self.assertEqual(
            self.hits(cronjob(annotations={collect.OPT_OUT_KEY: "exempt"}), job("j1", failed=1)), []
        )

    def test_a_system_namespace_cronjob_is_excluded(self):
        self.assertEqual(
            self.hits(cronjob(ns="kube-system"), job("j1", ns="kube-system", failed=1)), []
        )

    def test_a_gke_prefixed_namespace_cronjob_is_excluded(self):
        self.assertEqual(
            self.hits(cronjob(ns="gke-managed-cim"), job("j1", ns="gke-managed-cim", failed=1)), []
        )

    def test_an_addon_managed_cronjob_is_excluded(self):
        self.assertEqual(
            self.hits(
                cronjob(labels={"addonmanager.kubernetes.io/mode": "Reconcile"}), job("j1", failed=1)
            ),
            [],
        )

    def test_a_job_in_another_namespace_is_not_joined_to_it(self):
        # Names are namespace-scoped, so a same-named CronJob elsewhere is a
        # different object and its failures are not this one's evidence.
        self.assertEqual(self.hits(cronjob(ns="a"), job("j1", ns="b", failed=1)), [])

    def test_a_job_owned_by_something_else_is_not_joined_to_it(self):
        self.assertEqual(self.hits(cronjob(), job("j1", owner="another-cj", failed=1)), [])
        self.assertEqual(self.hits(cronjob(), job("j1", owner=None, failed=1)), [])

    def test_an_unparseable_timestamp_stays_quiet_rather_than_failing_the_cluster(self):
        # Ten other checks are riding on this collection. A timestamp in a shape
        # the parser does not expect costs the fleet this one CronJob, not the
        # cluster's whole result.
        self.assertEqual(self.hits(cronjob(last_schedule="whenever"), job("j1", failed=1)), [])
        self.assertEqual(self.hits(cronjob(last_success="whenever"), job("j1", failed=1)), [])

    def test_the_finding_names_what_reconciles_the_cronjob(self):
        # The live one is a Helm release, so its remediation has to say the fix
        # belongs in the chart rather than in `kubectl edit`.
        cj = cronjob("kube-agents-selfimprove", ns="kubeagents-system")
        cj["metadata"]["annotations"] = {
            "meta.helm.sh/release-name": "kube-agents",
            "meta.helm.sh/release-namespace": "kubeagents-system",
        }
        hits = self.hits(cj, job("j1", ns="kubeagents-system", owner="kube-agents-selfimprove", failed=1))
        self.assertIn("kube-agents", hits[0]["reconciler"])

    def test_the_shared_dump_does_not_leak_cronjobs_into_the_workload_checks(self):
        # 3.12 is why `cronjobs,jobs` were added to DUMP_COMMAND_KINDS. The
        # other eleven checks read `workloads`, which filters on WORKLOAD_KINDS,
        # so neither kind can reach them -- but that is a property of a filter
        # somewhere else, which is exactly the kind that stops being true.
        dump = dump_of(cronjob(), job("j1", failed=1), deployment("api"))
        self.assertEqual([wl["name"] for wl in collect.normalize_workloads(dump)], ["api"])

    def test_the_dump_asks_for_the_kinds_the_check_reads(self):
        self.assertIn("cronjobs", collect.DUMP_COMMAND_KINDS)
        self.assertIn("jobs", collect.DUMP_COMMAND_KINDS)


class TestRolloutDropsTraffic(unittest.TestCase):
    """§3.13. Endpoint removal and SIGTERM race, and only `preStop` delays one.

    Narrowed three ways on purpose. The raw shape -- a container with no
    `preStop` hook -- held on 24 of the ~30 non-system workloads in the fleet
    on 2026-09-05, which is a linter rule rather than an audit finding. Behind
    a serving Service, above one replica, and only for the containers the
    Service actually routes to, it holds on 6.
    """

    def wl(self, kind="Deployment", replicas=2, containers=None):
        d = deployment("api", **{"spec.replicas": replicas})
        d["kind"] = kind
        d["spec"]["template"]["metadata"] = {"labels": {"app": "api"}}
        if containers is not None:
            d["spec"]["template"]["spec"]["containers"] = containers
        return collect.normalize_workloads(dump_of(d))[0]

    def svc_ctx(self, ports=None):
        return context_of(
            services={
                "default": [
                    service("s", selector={"app": "api"}, **({"ports": ports} if ports else {}))
                ]
            }
        )

    def serving(self):
        return self.svc_ctx([{"name": "http", "port": 80, "targetPort": 8080}])

    def check(self, workload=None, context=None):
        return collect.check_rollout_drops_traffic(workload or self.wl(), context or self.serving())

    def test_a_multi_replica_serving_deployment_with_no_prestop_is_flagged(self):
        hit = self.check()
        self.assertIsNotNone(hit)
        self.assertEqual(hit["object"], "Deployment/api")

    def test_the_excerpt_names_the_containers_that_lack_the_hook(self):
        # The remediation is a block per container, so a finding that says only
        # "no preStop hook" leaves the reader to work out where it goes.
        hit = self.check(
            self.wl(
                containers=[
                    {"name": "app", "ports": [{"containerPort": 8080}]},
                    {"name": "sidecar", "ports": [{"containerPort": 8080}]},
                ]
            )
        )
        self.assertIn("app, sidecar", hit["excerpt"])

    def test_an_absent_grace_period_is_printed_as_the_kubernetes_default(self):
        # It bounds how long a hook may sleep, so the reader needs the number
        # that is in force -- and `None` describes no pod.
        self.assertIn(
            f"terminationGracePeriodSeconds={collect.DEFAULT_GRACE_PERIOD_S}", self.check()["excerpt"]
        )

    def test_a_declared_grace_period_is_printed_instead(self):
        wl = self.wl()
        wl["template"]["terminationGracePeriodSeconds"] = 5
        self.assertIn("terminationGracePeriodSeconds=5", self.check(wl)["excerpt"])

    def test_the_exposure_line_says_which_service_carries_the_traffic(self):
        self.assertIn("s[http]", self.check()["excerpt"])
        self.assertIn(collect._SCOPE_SERVING, self.check()["excerpt"])

    def test_a_daemonset_is_flagged_with_no_replica_test(self):
        # A DaemonSet has no `replicas`, and a node drain terminates its pod
        # exactly the way a rollout does.
        self.assertIsNotNone(self.check(self.wl(kind="DaemonSet", replicas=None)))

    def test_a_statefulset_above_one_replica_is_flagged(self):
        self.assertIsNotNone(self.check(self.wl(kind="StatefulSet")))

    def test_a_single_replica_deployment_is_left_to_3_11(self):
        # `single-replica` already publishes the whole rollout outage there, so
        # this would be a second finding about the same seconds.
        self.assertIsNone(self.check(self.wl(replicas=1)))

    def test_a_single_replica_statefulset_is_left_to_3_11(self):
        self.assertIsNone(self.check(self.wl(kind="StatefulSet", replicas=1)))

    def test_a_workload_no_service_selects_is_never_flagged(self):
        self.assertIsNone(self.check(context=context_of()))

    def test_a_metrics_only_service_is_never_flagged(self):
        # Prometheus retries a dropped scrape. There is no in-flight request to
        # lose, which is the entire claim this check makes.
        self.assertIsNone(self.check(context=self.svc_ctx([{"name": "metrics", "port": 9402}])))

    def test_a_service_declaring_no_ports_is_never_flagged(self):
        self.assertIsNone(self.check(context=self.svc_ctx()))

    def test_an_exporter_behind_a_sibling_metrics_service_is_not_named(self):
        # A serving and a metrics Service on one selector: only the serving
        # Service's targetPort is in the request path, so the exporter's missing
        # hook drops nothing even though a Service does route to it.
        wl = self.wl(
            containers=[
                {"name": "app", "ports": [{"containerPort": 8080}]},
                {"name": "exporter", "ports": [{"name": "metrics", "containerPort": 9102}]},
            ]
        )
        context = context_of(
            services={
                "default": [
                    service("s", selector={"app": "api"}, ports=[{"name": "http", "port": 80, "targetPort": 8080}]),
                    service("m", selector={"app": "api"}, ports=[{"name": "metrics", "port": 9102, "targetPort": 9102}]),
                ]
            }
        )
        hit = self.check(wl, context)
        self.assertIn("preStop hook: app\n", hit["excerpt"])
        self.assertNotIn("exporter", hit["excerpt"].splitlines()[0])
        readiness = collect.check_probes_readiness(wl, context)
        self.assertIn("readiness probe: app\n", readiness["excerpt"])

    def test_a_hook_on_every_serving_container_silences_it(self):
        self.assertIsNone(
            self.check(
                self.wl(
                    containers=[
                        {
                            "name": "app",
                            "ports": [{"containerPort": 8080}],
                            "lifecycle": {"preStop": {"exec": {"command": ["sleep", "5"]}}},
                        }
                    ]
                )
            )
        )

    def test_a_container_the_service_does_not_route_to_is_not_named(self):
        # Same narrowing the probe checks use: a log shipper on 9000 is not in
        # the request path, so its missing hook drops nothing.
        self.assertIsNone(
            self.check(
                self.wl(
                    containers=[
                        {
                            "name": "app",
                            "ports": [{"containerPort": 8080}],
                            "lifecycle": {"preStop": {"httpGet": {"path": "/quit", "port": 8080}}},
                        },
                        {"name": "logs", "ports": [{"containerPort": 9000}]},
                    ]
                )
            )
        )

    def test_a_self_healing_sidecar_is_not_named(self):
        # `istio-proxy` drains itself on SIGTERM; the shared exemption set says
        # so for the probe checks and means the same thing here.
        self.assertIsNone(
            self.check(
                self.wl(
                    containers=[
                        {
                            "name": "app",
                            "ports": [{"containerPort": 8080}],
                            "lifecycle": {"preStop": {"exec": {"command": ["sleep", "5"]}}},
                        },
                        {"name": "istio-proxy", "ports": [{"containerPort": 8080}]},
                    ]
                )
            )
        )

    def test_an_empty_lifecycle_block_is_not_a_hook(self):
        # `lifecycle: {}` with only a postStart under it is the near miss, and
        # it delays nothing.
        self.assertIsNotNone(
            self.check(
                self.wl(
                    containers=[
                        {
                            "name": "app",
                            "ports": [{"containerPort": 8080}],
                            "lifecycle": {"postStart": {"exec": {"command": ["true"]}}},
                        }
                    ]
                )
            )
        )


class TestPrestopOutlivesGrace(unittest.TestCase):
    """§3.20. The hook that is present and does nothing.

    §3.13 stops at "a `preStop` exists", so the shape where one exists and the
    grace period cuts it short is reported by nothing -- and it is §3.13's own
    remediation written without the second half of its instruction.
    """

    GRACE = 30

    def wl(self, containers, grace=GRACE, kind="Deployment"):
        d = deployment("api", **{"spec.replicas": 2})
        d["kind"] = kind
        d["spec"]["template"]["metadata"] = {"labels": {"app": "api"}}
        d["spec"]["template"]["spec"]["containers"] = containers
        if grace is not None:
            d["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] = grace
        return collect.normalize_workloads(dump_of(d))[0]

    def container(self, hook, name="app"):
        c = {"name": name, "ports": [{"containerPort": 8080}]}
        if hook is not None:
            c["lifecycle"] = {"preStop": hook}
        return c

    def check(self, containers, grace=GRACE):
        return collect.check_prestop_outlives_grace(self.wl(containers, grace), context_of())

    def test_a_native_sleep_equal_to_the_grace_period_is_flagged(self):
        # Equal, not merely greater: at `wait == grace` the kubelet SIGKILLs
        # with the hook still sleeping, so it is provably truncated.
        #
        # This case is the whole native arm, so do not relax it to `>`. The
        # apiserver validates `sleep.seconds` against the grace period and
        # rejects anything larger -- on GKE 1.35, `seconds: 31` under a 30s
        # ceiling is refused and `seconds: 30` applies -- so equality is the
        # only truncated native hook that can reach a cluster. The next test
        # covers a shape only reachable through a dump, not through admission.
        hit = self.check([self.container({"sleep": {"seconds": 30}})])
        self.assertIsNotNone(hit)
        self.assertEqual(hit["object"], "Deployment/api")

    def test_a_native_sleep_longer_than_the_grace_period_is_flagged(self):
        self.assertIsNotNone(self.check([self.container({"sleep": {"seconds": 45}})]))

    def test_a_shell_sleep_far_past_the_ceiling_is_flagged(self):
        # The asymmetry that makes the check worth having. Nothing validates
        # an `exec` hook against the grace period: `sleep 600` under a 10s
        # ceiling applies without complaint, where the same wait written as
        # `sleep: {seconds: 600}` is rejected outright. Every hook that runs
        # far past its ceiling is therefore a shell one.
        hit = self.check(
            [self.container({"exec": {"command": ["/bin/sh", "-c", "sleep 600"]}})], grace=10
        )
        self.assertIsNotNone(hit)
        self.assertIn("waits 600s", hit["excerpt"])
        self.assertIn("terminationGracePeriodSeconds=10", hit["excerpt"])

    def test_one_second_of_headroom_is_not_a_finding(self):
        # The check claims only what the kubelet guarantees. "Too little room
        # left for the process to shut down" needs a number this audit has not
        # got, so the predicate stops at the ceiling.
        self.assertIsNone(self.check([self.container({"sleep": {"seconds": 29}})]))

    def test_an_absent_grace_period_is_read_as_the_kubernetes_default(self):
        self.assertIsNotNone(
            self.check([self.container({"sleep": {"seconds": collect.DEFAULT_GRACE_PERIOD_S}})], grace=None)
        )
        self.assertIsNone(
            self.check(
                [self.container({"sleep": {"seconds": collect.DEFAULT_GRACE_PERIOD_S - 1}})], grace=None
            )
        )

    def test_a_shell_sleep_is_read_out_of_the_exec_command(self):
        hit = self.check([self.container({"exec": {"command": ["/bin/sh", "-c", "sleep 45"]}})])
        self.assertIsNotNone(hit)
        self.assertIn("sleep 45", hit["excerpt"])

    def test_the_longest_sleep_in_a_compound_command_is_the_one_read(self):
        # A hook that sleeps 60s somewhere in it cannot finish in less than
        # 60s whatever else the command does.
        self.assertIsNotNone(
            self.check(
                [self.container({"exec": {"command": ["/bin/sh", "-c", "sleep 5; drain; sleep 60"]}})]
            )
        )

    def test_a_fractional_sleep_is_not_read(self):
        # `sleep 0.5` is coreutils-only, so a duration parsed out of it would
        # be a guess about which shell the image ships.
        self.assertIsNone(
            self.check([self.container({"exec": {"command": ["/bin/sh", "-c", "sleep 0.5"]}})], grace=0)
        )

    def test_a_unit_suffixed_sleep_is_not_read(self):
        self.assertIsNone(
            self.check([self.container({"exec": {"command": ["/bin/sh", "-c", "sleep 1m"]}})])
        )

    def test_a_word_ending_in_sleep_is_not_a_sleep(self):
        self.assertIsNone(
            self.check([self.container({"exec": {"command": ["/bin/sh", "-c", "nosleep 60"]}})])
        )

    def test_an_httpget_handler_has_no_readable_duration(self):
        self.assertIsNone(self.check([self.container({"httpGet": {"path": "/quit", "port": 8080}})]))

    def test_a_tcpsocket_handler_has_no_readable_duration(self):
        self.assertIsNone(self.check([self.container({"tcpSocket": {"port": 8080}})]))

    def test_no_hook_at_all_is_left_to_313(self):
        self.assertIsNone(self.check([self.container(None)]))

    def test_an_exec_with_no_sleep_is_not_read(self):
        self.assertIsNone(
            self.check([self.container({"exec": {"command": ["/bin/sh", "-c", "curl -X POST /quit"]}})])
        )

    def test_the_excerpt_names_every_truncated_container_and_the_ceiling(self):
        hit = self.check(
            [
                self.container({"sleep": {"seconds": 40}}, name="app"),
                self.container({"sleep": {"seconds": 5}}, name="fine"),
                self.container({"sleep": {"seconds": 30}}, name="edge"),
            ]
        )
        self.assertIn("terminationGracePeriodSeconds=30", hit["excerpt"])
        self.assertIn("app waits 40s", hit["excerpt"])
        self.assertIn("edge waits 30s", hit["excerpt"])
        self.assertNotIn("fine", hit["excerpt"])

    def test_the_excerpt_names_which_spelling_carried_the_wait(self):
        # The remediation edits one of two fields and the reader has to know
        # which one is there.
        self.assertIn(
            "lifecycle.preStop.sleep.seconds", self.check([self.container({"sleep": {"seconds": 30}})])["excerpt"]
        )

    def test_a_native_sidecar_is_in_scope(self):
        # `_effective_containers` folds in `initContainers` with
        # `restartPolicy: Always`, and one of those terminates on the same
        # grace period as everything else in the pod.
        wl = self.wl([self.container(None)])
        wl["template"]["initContainers"] = [
            {"name": "proxy", "restartPolicy": "Always", "lifecycle": {"preStop": {"sleep": {"seconds": 30}}}}
        ]
        self.assertIsNotNone(collect.check_prestop_outlives_grace(wl, context_of()))

    def test_a_plain_init_container_is_not(self):
        wl = self.wl([self.container(None)])
        wl["template"]["initContainers"] = [
            {"name": "setup", "lifecycle": {"preStop": {"sleep": {"seconds": 30}}}}
        ]
        self.assertIsNone(collect.check_prestop_outlives_grace(wl, context_of()))

    def test_no_service_is_required_unlike_313(self):
        # An empty context has no Services in it at all. §3.13 needs one
        # because "has no preStop" is true of nearly every workload; this
        # predicate is already narrow enough to stand without one.
        self.assertIsNotNone(self.check([self.container({"sleep": {"seconds": 30}})]))

    def test_a_daemonset_is_in_scope(self):
        self.assertIsNotNone(
            collect.check_prestop_outlives_grace(
                self.wl([self.container({"sleep": {"seconds": 30}})], kind="DaemonSet"), context_of()
            )
        )

    def test_a_zero_grace_period_truncates_any_hook(self):
        self.assertIsNotNone(self.check([self.container({"sleep": {"seconds": 1}})], grace=0))


class TestMaxSurgeCount(unittest.TestCase):
    """`maxSurge` resolves the opposite way to `maxUnavailable`, and §3.21
    rests on that. Rounding a percentage down instead would clear every
    single-replica Deployment, which is every one this check is for."""

    def test_the_default_of_twenty_five_percent_of_one_replica_rounds_up_to_one(self):
        self.assertEqual(collect._max_surge_count({}, 1), 1)

    def test_twenty_five_percent_of_four_replicas_is_one(self):
        self.assertEqual(collect._max_surge_count({"maxSurge": "25%"}, 4), 1)

    def test_fifty_percent_of_three_replicas_rounds_up_to_two(self):
        self.assertEqual(collect._max_surge_count({"maxSurge": "50%"}, 3), 2)

    def test_an_absolute_zero_is_zero_rather_than_the_default(self):
        self.assertEqual(collect._max_surge_count({"maxSurge": 0}, 1), 0)

    def test_an_absolute_count_is_read_verbatim(self):
        self.assertEqual(collect._max_surge_count({"maxSurge": 3}, 1), 3)

    def test_an_unparseable_value_falls_back_to_the_kubernetes_default(self):
        # Not to nought. A field this collector cannot read is a field it
        # knows nothing about, and reading it as "no surge" would clear the
        # workload on the strength of a parse failure.
        self.assertEqual(collect._max_surge_count({"maxSurge": "lots"}, 1), 1)
        self.assertEqual(collect._max_surge_count({"maxSurge": "1x%"}, 4), 1)


def pvc(name, ns="default", modes=("ReadWriteOnce",), storage_class="standard-rwo", status_modes=None):
    doc = {
        "kind": "PersistentVolumeClaim",
        "metadata": {"namespace": ns, "name": name},
        "spec": {"accessModes": list(modes), "storageClassName": storage_class},
    }
    if status_modes is not None:
        doc["status"] = {"accessModes": list(status_modes)}
    return doc


class TestRwoClaimContended(unittest.TestCase):
    """§3.21. Two pods sent to a volume one node holds.

    The rollout arm is the one worth the check: a Deployment, a claim, and the
    strategy Kubernetes supplies when none is written are the whole recipe, and
    the result is a workload that reports `Available` throughout while silently
    refusing every update it is given.
    """

    def wl(self, *, replicas=1, strategy=None, volumes=None, kind="Deployment"):
        d = deployment("grafana", **{"spec.replicas": replicas})
        d["kind"] = kind
        if strategy is not None:
            d["spec"]["strategy"] = strategy
        d["spec"]["template"]["spec"]["volumes"] = list(volumes or [])
        return collect.normalize_workloads(dump_of(d))[0]

    def claim_volume(self, claim_name="grafana-data"):
        return {"name": "data", "persistentVolumeClaim": {"claimName": claim_name}}

    def ctx(self, *claims):
        return context_of(claims=collect.claims_by_key(dump_of(*claims)))

    def check(self, workload, *claims):
        return collect.check_rwo_claim_contended(workload, self.ctx(*claims))

    def test_one_replica_under_the_default_strategy_is_flagged(self):
        # No `strategy` key at all, which is what most Deployments carry. The
        # controller supplies RollingUpdate with maxSurge 25%, that rounds up
        # to one extra pod, and the two contend.
        hit = self.check(self.wl(volumes=[self.claim_volume()]), pvc("grafana-data"))
        self.assertIsNotNone(hit)
        self.assertEqual(hit["object"], "Deployment/grafana")
        self.assertIn("maxSurge resolving to 1", hit["excerpt"])
        self.assertIn("grafana-data", hit["excerpt"])
        self.assertIs(hit["impact"], collect._IMPACT_RWO_ROLLOUT_DEADLOCK)

    def test_the_excerpt_names_the_access_modes_and_the_storage_class(self):
        # Both feed the remediation. The modes are the claim this finding
        # makes, and the class is what tells an owner whether moving to
        # ReadWriteMany is cheap on their cluster.
        hit = self.check(
            self.wl(volumes=[self.claim_volume()]), pvc("grafana-data", storage_class="premium-rwo")
        )
        self.assertIn("ReadWriteOnce", hit["excerpt"])
        self.assertIn("storageClass premium-rwo", hit["excerpt"])

    def test_a_claim_with_no_storage_class_says_so_rather_than_printing_nothing(self):
        hit = self.check(
            self.wl(volumes=[self.claim_volume()]), pvc("grafana-data", storage_class=None)
        )
        self.assertIn("(cluster default)", hit["excerpt"])

    def test_recreate_is_the_fix_already_applied_and_is_not_flagged(self):
        self.assertIsNone(
            self.check(
                self.wl(strategy={"type": "Recreate"}, volumes=[self.claim_volume()]),
                pvc("grafana-data"),
            )
        )

    def test_one_replica_with_max_surge_zero_is_not_flagged(self):
        # The old pod is removed before the replacement starts, so the volume
        # is free by the time anything asks for it.
        self.assertIsNone(
            self.check(
                self.wl(
                    strategy={"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0}},
                    volumes=[self.claim_volume()],
                ),
                pvc("grafana-data"),
            )
        )

    def test_more_than_one_replica_is_the_other_arm_and_carries_its_own_impact(self):
        hit = self.check(self.wl(replicas=3, volumes=[self.claim_volume()]), pvc("grafana-data"))
        self.assertIsNotNone(hit)
        self.assertIn("replicas=3", hit["excerpt"])
        self.assertIs(hit["impact"], collect._IMPACT_RWO_REPLICAS_PINNED)

    def test_more_than_one_replica_is_flagged_even_where_nothing_surges(self):
        # Above one replica the pods coexist by definition rather than during
        # a rollout, so `maxSurge: 0` changes nothing about the contention.
        hit = self.check(
            self.wl(
                replicas=3,
                strategy={"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0}},
                volumes=[self.claim_volume()],
            ),
            pvc("grafana-data"),
        )
        self.assertIsNotNone(hit)
        self.assertIs(hit["impact"], collect._IMPACT_RWO_REPLICAS_PINNED)

    def test_read_write_once_pod_is_the_stricter_mode_and_is_flagged(self):
        self.assertIsNotNone(
            self.check(
                self.wl(volumes=[self.claim_volume()]),
                pvc("grafana-data", modes=("ReadWriteOncePod",)),
            )
        )

    def test_read_write_many_is_not_flagged(self):
        self.assertIsNone(
            self.check(
                self.wl(volumes=[self.claim_volume()]),
                pvc("grafana-data", modes=("ReadWriteMany",)),
            )
        )

    def test_a_claim_listing_read_write_many_alongside_once_is_not_flagged(self):
        # Every mode has to be exclusive. A second node can attach this one,
        # so nothing deadlocks and there is no finding.
        self.assertIsNone(
            self.check(
                self.wl(volumes=[self.claim_volume()]),
                pvc("grafana-data", modes=("ReadWriteOnce", "ReadWriteMany")),
            )
        )

    def test_the_bound_volumes_status_modes_win_over_the_request(self):
        # `spec.accessModes` is what was asked for; `status.accessModes` is
        # what the bound volume supports. Reading the request here would
        # report a Filestore-backed claim as exclusive.
        self.assertIsNone(
            self.check(
                self.wl(volumes=[self.claim_volume()]),
                pvc("grafana-data", modes=("ReadWriteOnce",), status_modes=("ReadWriteMany",)),
            )
        )

    def test_an_unbound_claim_with_no_status_falls_back_to_the_request(self):
        self.assertIsNotNone(
            self.check(self.wl(volumes=[self.claim_volume()]), pvc("grafana-data"))
        )

    def test_a_claim_the_dump_does_not_hold_is_skipped_rather_than_assumed(self):
        # Guessing ReadWriteOnce because it is the common default would send a
        # pull request to change a rollout strategy over a volume that may
        # well be shared.
        self.assertIsNone(self.check(self.wl(volumes=[self.claim_volume("elsewhere")]), pvc("grafana-data")))

    def test_a_claim_of_the_same_name_in_another_namespace_is_not_read(self):
        self.assertIsNone(
            self.check(self.wl(volumes=[self.claim_volume()]), pvc("grafana-data", ns="other"))
        )

    def test_a_generic_ephemeral_volume_is_not_flagged(self):
        # The controller creates one claim per pod, so two pods never contend.
        # Reading only `persistentVolumeClaim.claimName` excludes these for
        # free, and this test is what keeps that true.
        volume = {"name": "scratch", "ephemeral": {"volumeClaimTemplate": {"spec": {"accessModes": ["ReadWriteOnce"]}}}}
        self.assertIsNone(self.check(self.wl(volumes=[volume]), pvc("grafana-data")))

    def test_a_workload_with_no_volumes_is_not_flagged(self):
        self.assertIsNone(self.check(self.wl(volumes=[]), pvc("grafana-data")))

    def test_a_deployment_scaled_to_zero_is_dropped_by_s5_before_the_check_runs(self):
        scaled_to_zero = deployment("grafana", **{"spec.replicas": 0})
        scaled_to_zero["spec"]["template"]["spec"]["volumes"] = [self.claim_volume()]
        self.assertEqual(collect.normalize_workloads(dump_of(scaled_to_zero)), [])

    def test_a_zero_replica_workload_reaching_the_check_anyway_is_not_flagged(self):
        # The guard inside the check, exercised directly because S5 above means
        # nothing assembled by `normalize_workloads` can reach it. Without it
        # the two arms would have to reason about nought replicas.
        hand_built = {
            "kind": "Deployment",
            "ns": "default",
            "name": "grafana",
            "spec": {"replicas": 0},
            "template": {"volumes": [self.claim_volume()]},
        }
        self.assertIsNone(self.check(hand_built, pvc("grafana-data")))

    def test_a_statefulset_is_not_flagged(self):
        # `volumeClaimTemplates` give each replica its own claim, and the
        # update strategy replaces one member at a time rather than surging.
        self.assertIsNone(
            self.check(
                self.wl(kind="StatefulSet", volumes=[self.claim_volume()]), pvc("grafana-data")
            )
        )

    def test_it_never_fires_on_the_same_object_as_strategy_causes_downtime(self):
        # 3.14 reports `Recreate` above one replica as a full outage, so the
        # single-replica remediation this check writes must not be reachable
        # where 3.14 would then pick the object up. The two exclusions are
        # complementary by construction and this is the test that says so.
        ctx = self.ctx(pvc("grafana-data"))
        one = self.wl(replicas=1, volumes=[self.claim_volume()])
        self.assertIsNotNone(collect.check_rwo_claim_contended(one, ctx))
        self.assertIsNone(collect.check_strategy_causes_downtime(one, ctx))
        many = self.wl(replicas=3, strategy={"type": "Recreate"}, volumes=[self.claim_volume()])
        self.assertIsNone(collect.check_rwo_claim_contended(many, ctx))
        self.assertIsNotNone(collect.check_strategy_causes_downtime(many, ctx))

    def test_the_roster_entry_matches_what_the_sop_promises(self):
        spec = next(c for c in collect.OBTAINABILITY_CHECKS if c.slug == "rwo-claim-contended")
        self.assertEqual(spec.kind, "workload")
        self.assertEqual(spec.severity, "major")
        self.assertIsNone(spec.autopilot_severity)

    def test_the_dump_asks_for_the_claims_this_check_reads(self):
        # A check reading a kind the dump does not collect finds nothing and
        # reports a clean cluster, which is the failure mode this line guards.
        self.assertIn("persistentvolumeclaims", collect.DUMP_COMMAND_KINDS)


class TestStrategyCausesDowntime(unittest.TestCase):
    """§3.14. The gap 3.11 leaves open.

    `check_single_replica` returns None on `strategy.type == Recreate` *and* on
    any `replicas != 1`, so a three-replica Recreate Deployment -- a guaranteed
    full outage on every deploy -- fell through both arms and nothing else in
    the roster reads `spec.strategy` at all.
    """

    def wl(self, kind="Deployment", replicas=3, strategy=None):
        d = deployment("api", **{"spec.replicas": replicas})
        d["kind"] = kind
        d["spec"]["template"]["metadata"] = {"labels": {"app": "api"}}
        if strategy is not None:
            d["spec"]["strategy"] = strategy
        return collect.normalize_workloads(dump_of(d))[0]

    def rolling(self, max_unavailable):
        return {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": max_unavailable}}

    def check(self, workload=None, context=None):
        return collect.check_strategy_causes_downtime(workload or self.wl(), context or context_of())

    def serving_ctx(self):
        return context_of(
            services={
                "default": [
                    service("s", selector={"app": "api"}, ports=[{"name": "http", "port": 80}])
                ]
            }
        )

    def test_recreate_above_one_replica_is_flagged(self):
        hit = self.check(self.wl(strategy={"type": "Recreate"}))
        self.assertIsNotNone(hit)
        self.assertEqual(hit["object"], "Deployment/api")
        self.assertIn("strategy.type=Recreate, replicas=3", hit["excerpt"])

    def test_max_unavailable_equal_to_the_replica_count_is_flagged(self):
        hit = self.check(self.wl(strategy=self.rolling(3)))
        self.assertIn("maxUnavailable=3 resolves to 3 of 3 replicas", hit["excerpt"])

    def test_max_unavailable_above_the_replica_count_is_flagged(self):
        # Legal, and it means the same outage as `=replicas`.
        self.assertIsNotNone(self.check(self.wl(strategy=self.rolling(5))))

    def test_a_hundred_percent_is_flagged(self):
        hit = self.check(self.wl(strategy=self.rolling("100%")))
        self.assertIn("maxUnavailable=100% resolves to 3 of 3 replicas", hit["excerpt"])

    def test_a_percentage_rounds_down_the_way_kubernetes_does(self):
        # 99% of 3 is 2.97 and Kubernetes floors `maxUnavailable`, so one
        # replica stays up and there is no outage to report. Rounding the other
        # way would invent a finding on every workload set to 99%.
        self.assertIsNone(self.check(self.wl(strategy=self.rolling("99%"))))
        self.assertIsNone(self.check(self.wl(replicas=2, strategy=self.rolling("50%"))))

    def test_a_service_backed_workload_takes_the_higher_severity(self):
        hit = self.check(self.wl(strategy={"type": "Recreate"}), self.serving_ctx())
        self.assertEqual(hit["severity"], "major")
        self.assertIn("s[http]", hit["excerpt"])

    def test_a_workload_nothing_routes_to_is_downgraded(self):
        # A batch worker behind no Service still stops working during the
        # rollout, which is worth saying and is not an outage anyone sees.
        hit = self.check(self.wl(strategy={"type": "Recreate"}))
        self.assertEqual(hit["severity"], "minor")
        self.assertNotIn("s[", hit["excerpt"])

    def test_a_metrics_only_service_is_downgraded_too(self):
        ctx = context_of(
            services={
                "default": [
                    service("s", selector={"app": "api"}, ports=[{"name": "metrics", "port": 9402}])
                ]
            }
        )
        self.assertEqual(self.check(self.wl(strategy={"type": "Recreate"}), ctx)["severity"], "minor")

    def test_a_single_replica_deployment_is_left_to_3_11(self):
        self.assertIsNone(self.check(self.wl(replicas=1, strategy={"type": "Recreate"})))

    def test_the_default_strategy_is_never_flagged(self):
        # RollingUpdate with nothing under it defaults to 25%, which is the
        # shape the overwhelming majority of the fleet has.
        self.assertIsNone(self.check(self.wl()))
        self.assertIsNone(self.check(self.wl(strategy={"type": "RollingUpdate"})))

    def test_a_survivable_max_unavailable_is_never_flagged(self):
        self.assertIsNone(self.check(self.wl(strategy=self.rolling(1))))
        self.assertIsNone(self.check(self.wl(strategy=self.rolling(2))))

    def test_a_statefulset_is_never_flagged(self):
        # `spec.strategy` is a Deployment field; a StatefulSet's is
        # `updateStrategy` and cannot express this.
        self.assertIsNone(self.check(self.wl(kind="StatefulSet", strategy={"type": "Recreate"})))

    def test_a_daemonset_is_never_flagged(self):
        self.assertIsNone(self.check(self.wl(kind="DaemonSet", strategy={"type": "Recreate"})))

    def test_an_unparseable_max_unavailable_stays_quiet(self):
        # A templating accident leaves `{{ .Values.surge }}` in the field. It
        # is not evidence of an outage, so it is not reported as one.
        self.assertIsNone(self.check(self.wl(strategy=self.rolling("{{ .Values.mu }}"))))
        self.assertIsNone(self.check(self.wl(strategy=self.rolling("many%"))))


class TestCronjobRunsOverlap(unittest.TestCase):
    """§3.15. Measured from the retained Jobs, never predicted from the cron.

    The collector has no clock and does not parse cron expressions, so this
    compares the longest completed run against the widest gap between retained
    Job creations. A hit therefore says the schedule *has already* overlapped,
    not that its policy would let it.
    """

    def hits(self, *items):
        return collect.check_cronjob_runs_overlap(context_of(dump_of(*items)))

    def runs(self, *spans, **cj):
        """A CronJob plus one Job per (created, started, completed) triple."""
        return [cronjob(**cj)] + [
            job(f"j{i}", created=c, started=s, completed=e) for i, (c, s, e) in enumerate(spans)
        ]

    def overlapping(self, **cj):
        # Fires every 10 minutes; the middle run takes 15 and is still going
        # when the next one starts.
        return self.runs(
            ("2026-09-06T20:00:00Z", "2026-09-06T20:00:00Z", "2026-09-06T20:05:00Z"),
            ("2026-09-06T20:10:00Z", "2026-09-06T20:10:00Z", "2026-09-06T20:25:00Z"),
            ("2026-09-06T20:20:00Z", "2026-09-06T20:20:00Z", "2026-09-06T20:24:00Z"),
            **cj,
        )

    def test_a_run_longer_than_its_period_is_flagged(self):
        hits = self.hits(*self.overlapping())
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "CronJob/cj")
        self.assertEqual(hits[0]["namespace"], "default")

    def test_the_excerpt_carries_both_measurements_and_the_job_it_read_them_from(self):
        # The remediation is a one-field edit whose whole justification is that
        # the run outlasts the period, so the finding has to show both numbers
        # and name the Job a reader can go and check.
        excerpt = self.hits(*self.overlapping())[0]["excerpt"]
        self.assertIn("slowest retained run j1 took 15.0m", excerpt)
        self.assertIn("observed period of 10.0m", excerpt)
        self.assertIn("across 3 retained Job(s)", excerpt)
        self.assertIn("'0 * * * *'", excerpt)

    def test_an_unset_concurrency_policy_is_named_as_a_default(self):
        # Almost every hit will be this one: `Allow` is what you get by writing
        # nothing, so the finding says the field is absent rather than implying
        # someone chose it.
        self.assertIn("concurrencyPolicy unset (defaults to Allow)", self.hits(*self.overlapping())[0]["excerpt"])

    def test_an_explicit_allow_is_named_as_a_choice(self):
        items = self.overlapping()
        items[0]["spec"]["concurrencyPolicy"] = "Allow"
        excerpt = self.hits(*items)[0]["excerpt"]
        self.assertIn("concurrencyPolicy=Allow", excerpt)
        self.assertNotIn("unset", excerpt)

    def test_a_run_exactly_as_long_as_the_period_is_flagged(self):
        # The next run starts as this one ends; the floor is inclusive because
        # that is already a pile-up at any jitter.
        self.assertEqual(
            len(
                self.hits(
                    *self.runs(
                        ("2026-09-06T20:00:00Z", "2026-09-06T20:00:00Z", "2026-09-06T20:10:00Z"),
                        ("2026-09-06T20:10:00Z", "2026-09-06T20:10:00Z", "2026-09-06T20:20:00Z"),
                        ("2026-09-06T20:20:00Z", "2026-09-06T20:20:00Z", "2026-09-06T20:30:00Z"),
                    )
                )
            ),
            1,
        )

    def test_forbid_is_never_flagged(self):
        items = self.overlapping()
        items[0]["spec"]["concurrencyPolicy"] = "Forbid"
        self.assertEqual(self.hits(*items), [])

    def test_replace_is_never_flagged(self):
        # Both bound the pile-up. Which one is right is a real decision about
        # the workload, and this check does not have the standing to make it.
        items = self.overlapping()
        items[0]["spec"]["concurrencyPolicy"] = "Replace"
        self.assertEqual(self.hits(*items), [])

    def test_a_suspended_cronjob_is_never_flagged(self):
        self.assertEqual(self.hits(*self.overlapping(suspend=True)), [])

    def test_a_run_shorter_than_its_period_is_never_flagged(self):
        self.assertEqual(
            self.hits(
                *self.runs(
                    ("2026-09-06T20:00:00Z", "2026-09-06T20:00:00Z", "2026-09-06T20:01:00Z"),
                    ("2026-09-06T20:10:00Z", "2026-09-06T20:10:00Z", "2026-09-06T20:12:00Z"),
                    ("2026-09-06T20:20:00Z", "2026-09-06T20:20:00Z", "2026-09-06T20:21:00Z"),
                )
            ),
            [],
        )

    def test_two_retained_jobs_are_too_few_to_read_a_period_from(self):
        # One gap is not a period: `successfulJobsHistoryLimit` defaults to 3,
        # so two Jobs usually means the schedule has only just started.
        self.assertEqual(
            self.hits(
                *self.runs(
                    ("2026-09-06T20:00:00Z", "2026-09-06T20:00:00Z", "2026-09-06T20:30:00Z"),
                    ("2026-09-06T20:10:00Z", "2026-09-06T20:10:00Z", "2026-09-06T20:40:00Z"),
                )
            ),
            [],
        )

    def test_the_widest_gap_is_the_period_not_the_narrowest(self):
        # Retained Jobs are a sample with holes in it -- a deleted Job, a
        # missed schedule -- and the narrowest gap would read an hourly job
        # that once fired twice in a minute as a 1-minute schedule. Widest
        # overestimates the period, which makes the check quieter.
        self.assertEqual(
            self.hits(
                *self.runs(
                    ("2026-09-06T20:00:00Z", "2026-09-06T20:00:00Z", "2026-09-06T20:05:00Z"),
                    ("2026-09-06T20:01:00Z", "2026-09-06T20:01:00Z", "2026-09-06T20:06:00Z"),
                    ("2026-09-06T21:00:00Z", "2026-09-06T21:00:00Z", "2026-09-06T21:05:00Z"),
                )
            ),
            [],
        )

    def test_a_history_with_nothing_completed_says_nothing(self):
        # A Job with a `startTime` and no `completionTime` is either wedged or
        # slow, and guessing which is how this check would invent findings.
        self.assertEqual(
            self.hits(
                cronjob(),
                job("j0", created="2026-09-06T20:00:00Z", started="2026-09-06T20:00:00Z"),
                job("j1", created="2026-09-06T20:10:00Z", started="2026-09-06T20:10:00Z"),
                job("j2", created="2026-09-06T20:20:00Z", started="2026-09-06T20:20:00Z"),
            ),
            [],
        )

    def test_a_completion_before_its_start_is_ignored(self):
        # Clock skew between the apiserver and a node writes these, and a
        # negative duration is not a long run.
        self.assertEqual(
            self.hits(
                *self.runs(
                    ("2026-09-06T20:00:00Z", "2026-09-06T20:05:00Z", "2026-09-06T20:00:00Z"),
                    ("2026-09-06T20:10:00Z", "2026-09-06T20:15:00Z", "2026-09-06T20:10:00Z"),
                    ("2026-09-06T20:20:00Z", "2026-09-06T20:25:00Z", "2026-09-06T20:20:00Z"),
                )
            ),
            [],
        )

    def test_an_unparseable_creation_timestamp_is_dropped_from_the_sample(self):
        # Two parseable stamps left is under the floor, so the CronJob goes
        # unreported rather than taking the cluster's whole collection down.
        self.assertEqual(
            self.hits(
                *self.runs(
                    ("whenever", "2026-09-06T20:00:00Z", "2026-09-06T20:30:00Z"),
                    ("2026-09-06T20:10:00Z", "2026-09-06T20:10:00Z", "2026-09-06T20:40:00Z"),
                    ("2026-09-06T20:20:00Z", "2026-09-06T20:20:00Z", "2026-09-06T20:50:00Z"),
                )
            ),
            [],
        )

    def test_the_shared_joiner_still_applies_the_standard_exclusions(self):
        # S1/S2/S4 live in `cronjobs_with_jobs`, so this check inherits them
        # rather than restating them -- which is only true while it keeps
        # reading `context["cronjobs"]`.
        self.assertEqual(self.hits(*self.overlapping(ns="kube-system")), [])
        self.assertEqual(self.hits(*self.overlapping(labels={collect.OPT_OUT_KEY: "exempt"})), [])
        self.assertEqual(
            self.hits(*self.overlapping(annotations={collect.OPT_OUT_KEY: "exempt"})), []
        )

    def test_the_finding_names_what_reconciles_the_cronjob(self):
        items = self.overlapping()
        items[0]["metadata"]["annotations"] = {
            "meta.helm.sh/release-name": "kube-agents",
            "meta.helm.sh/release-namespace": "kubeagents-system",
        }
        self.assertIn("kube-agents", self.hits(*items)[0]["reconciler"])

    def test_a_failing_schedule_can_be_both_this_and_3_12(self):
        # The two checks read different fields and name different fixes, so a
        # CronJob whose runs overlap *and* have stopped succeeding owes both
        # findings rather than whichever one is evaluated first.
        items = self.overlapping()
        for item in items[1:]:
            item["status"]["failed"] = 1
            item["status"].pop("active", None)  # it has a completionTime
            item["status"]["conditions"] = [{"type": "Failed", "status": "True"}]
        self.assertEqual(len(self.hits(*items)), 1)
        self.assertEqual(len(collect.check_schedule_never_succeeds(context_of(dump_of(*items)))), 1)


def endpointslice(
    service_name, ns="default", addresses=1, terminating=0, ports=None, nodes=None, unready=(), zones=None
):
    """An EndpointSlice with `addresses` ready endpoints and `terminating`
    endpoints on their way out.

    `ports` is the list of service-port names the controller managed to
    resolve, which is what it writes: a name it could not find on the pod is
    left out of the slice entirely rather than written with a null number.

    `nodes` gives a `nodeName` to each ready endpoint in order, which is what
    §3.19 reads; omitting it produces the pre-§3.19 shape, where no endpoint
    names a node at all. `unready` is the indices among them the controller
    marked `ready: False`."""
    endpoints = [{"addresses": [f"10.0.0.{i}"]} for i in range(addresses)]
    for i, endpoint in enumerate(endpoints):
        if nodes is not None and i < len(nodes):
            endpoint["nodeName"] = nodes[i]
        if zones is not None and i < len(zones):
            endpoint["zone"] = zones[i]
        if i in unready:
            endpoint["conditions"] = {"ready": False}
    endpoints += [
        {"addresses": [f"10.0.1.{i}"], "conditions": {"terminating": True}}
        for i in range(terminating)
    ]
    slice_ = {
        "kind": "EndpointSlice",
        "metadata": {
            "namespace": ns,
            "name": f"{service_name}-abcde",
            "labels": {collect.ENDPOINTSLICE_SERVICE_LABEL: service_name},
        },
        "endpoints": endpoints,
    }
    if ports is not None:
        slice_["ports"] = [{"name": p, "port": 8080, "protocol": "TCP"} for p in ports]
    return slice_


class TestServiceSelectsNothing(unittest.TestCase):
    """§3.16. A Service is an unvalidated label query, and one that matches
    nothing looks exactly like one that matches everything."""

    def hits(self, *items):
        return collect.check_service_selects_nothing(context_of(dump_of(*items)))

    def svc(self, **kwargs):
        kwargs.setdefault("selector", {"app": "api"})
        return service("api", **kwargs)

    def test_a_selector_matching_nothing_is_flagged(self):
        hits = self.hits(self.svc())
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "Service/api")
        self.assertEqual(hits[0]["namespace"], "default")

    def test_a_service_with_endpoints_is_never_flagged(self):
        self.assertEqual(self.hits(self.svc(), endpointslice("api")), [])

    def test_only_terminating_endpoints_still_counts_as_nothing(self):
        # A drained pod is still listed until it goes away, so counting it
        # would let a Service that genuinely resolves to nothing read healthy
        # for as long as the last old pod lingers.
        self.assertEqual(len(self.hits(self.svc(), endpointslice("api", addresses=0, terminating=2))), 1)

    def test_one_live_endpoint_beside_terminating_ones_is_enough(self):
        self.assertEqual(self.hits(self.svc(), endpointslice("api", addresses=1, terminating=2)), [])

    def test_slices_are_joined_by_name_and_namespace(self):
        # Same Service name in another namespace must not answer for this one.
        self.assertEqual(len(self.hits(self.svc(), endpointslice("api", ns="other"))), 1)

    def test_a_service_with_no_selector_is_never_flagged(self):
        # Hand-managed endpoints are the sanctioned way to point at an address
        # outside the cluster; there is no query to be wrong.
        self.assertEqual(self.hits(service("api")), [])

    def test_an_externalname_service_is_never_flagged(self):
        self.assertEqual(
            self.hits(service("api", selector={"app": "api"}, svc_type="ExternalName")), []
        )

    def test_a_backend_deliberately_scaled_to_zero_excuses_it(self):
        # Not broken, parked. S5 drops zeroed workloads from the audited set,
        # so this has to read the dump or every scaled-down Service in the
        # fleet is reported as a black hole.
        parked = deployment("api", **{"spec.replicas": 0})
        parked["spec"]["template"]["metadata"] = {"labels": {"app": "api"}}
        self.assertEqual(self.hits(self.svc(), parked), [])

    def test_a_running_backend_that_the_selector_misses_is_still_flagged(self):
        # The whole failure mode: a workload is there, the labels do not match,
        # and nothing in the cluster says so.
        running = deployment("api", **{"spec.replicas": 2})
        running["spec"]["template"]["metadata"] = {"labels": {"app": "api-v2"}}
        self.assertEqual(len(self.hits(self.svc(), running)), 1)

    def test_a_zeroed_workload_in_another_namespace_does_not_excuse_it(self):
        parked = deployment("api", ns="other", **{"spec.replicas": 0})
        parked["spec"]["template"]["metadata"] = {"labels": {"app": "api"}}
        self.assertEqual(len(self.hits(self.svc(), parked)), 1)

    def test_an_externally_published_service_is_critical(self):
        for svc_type in ("LoadBalancer", "NodePort"):
            with self.subTest(svc_type=svc_type):
                self.assertEqual(self.hits(self.svc(svc_type=svc_type))[0]["severity"], "critical")

    def test_a_clusterip_service_is_major(self):
        self.assertEqual(self.hits(self.svc())[0]["severity"], "major")

    def test_the_excerpt_carries_the_selector_the_type_and_the_ports(self):
        # The remediation is an edit to one of those three fields, so the
        # finding shows all of them rather than sending the reader to `kubectl`.
        excerpt = self.hits(self.svc(svc_type="LoadBalancer", ports=[{"port": 80}]))[0]["excerpt"]
        self.assertIn("type=LoadBalancer", excerpt)
        self.assertIn("port(s)=80", excerpt)
        self.assertIn("'app=api'", excerpt)

    def test_an_unstated_type_reads_as_clusterip(self):
        bare = {"kind": "Service", "metadata": {"namespace": "default", "name": "api"}, "spec": {"selector": {"app": "api"}}}
        self.assertIn("type=ClusterIP", self.hits(bare)[0]["excerpt"])

    def test_the_standard_exclusions_apply(self):
        self.assertEqual(self.hits(self.svc(ns="kube-system")), [])
        exempt = self.svc()
        exempt["metadata"]["labels"] = {collect.OPT_OUT_KEY: "exempt"}
        self.assertEqual(self.hits(exempt), [])
        annotated = self.svc()
        annotated["metadata"]["annotations"] = {collect.OPT_OUT_KEY: "exempt"}
        self.assertEqual(self.hits(annotated), [])
        addon = self.svc()
        addon["metadata"]["labels"] = {"addonmanager.kubernetes.io/mode": "Reconcile"}
        self.assertEqual(self.hits(addon), [])

    def test_the_finding_names_what_reconciles_the_service(self):
        helm = self.svc()
        helm["metadata"]["annotations"] = {
            "meta.helm.sh/release-name": "kube-agents",
            "meta.helm.sh/release-namespace": "kubeagents-system",
        }
        self.assertIn("kube-agents", self.hits(helm)[0]["reconciler"])

    def test_a_headless_service_is_treated_like_any_other(self):
        # `clusterIP: None` still selects pods and still resolves to their
        # addresses, so one selecting nothing breaks its clients the same way.
        headless = self.svc()
        headless["spec"]["clusterIP"] = "None"
        self.assertEqual(len(self.hits(headless)), 1)


class TestServicePortUnresolved(unittest.TestCase):
    """§3.17. The selector matches, the pods are up, the slices list their
    addresses -- and the port the Service asked for by name is not in them."""

    def hits(self, *items):
        return collect.check_service_port_unresolved(context_of(dump_of(*items)))

    def svc(self, target="http", port_name="web", **kwargs):
        kwargs.setdefault("selector", {"app": "api"})
        kwargs.setdefault("ports", [{"name": port_name, "port": 80, "targetPort": target}])
        return service("api", **kwargs)

    def backend(self, *port_names):
        dep = deployment("api")
        dep["spec"]["template"]["metadata"] = {"labels": {"app": "api"}}
        dep["spec"]["template"]["spec"]["containers"][0]["ports"] = [
            {"name": n, "containerPort": 8080} for n in port_names
        ]
        return dep

    def test_a_named_target_missing_from_the_slice_is_flagged(self):
        hits = self.hits(self.svc(), endpointslice("api", ports=[]), self.backend("grpc"))
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "Service/api")

    def test_a_resolved_name_is_never_flagged(self):
        self.assertEqual(self.hits(self.svc(), endpointslice("api", ports=["web"])), [])

    def test_a_numeric_target_cannot_fail_this_way(self):
        # `containerPort` is informational -- traffic goes to the number
        # whether or not a container declares it -- so flagging one would
        # report every Service that omits a redundant declaration.
        self.assertEqual(self.hits(self.svc(target=8080), endpointslice("api", ports=[])), [])

    def test_a_service_with_no_endpoints_belongs_to_3_16(self):
        self.assertEqual(self.hits(self.svc(), endpointslice("api", addresses=0, ports=[])), [])

    def test_terminating_endpoints_alone_do_not_open_this_check(self):
        self.assertEqual(
            self.hits(self.svc(), endpointslice("api", addresses=0, terminating=2, ports=[])), []
        )

    def test_a_slice_with_no_ports_key_at_all_still_flags(self):
        # A headless Service with no `spec.ports` gets an empty list; one that
        # declares a port the controller could not resolve gets the same, so
        # the absent key has to read as unresolved rather than as unknown.
        self.assertEqual(len(self.hits(self.svc(), endpointslice("api"))), 1)

    def test_the_wire_shape_of_an_empty_port_list_is_null(self):
        # What the apiserver actually serves for a slice whose every port went
        # unresolved, observed on GKE 1.35: the key is present and `null`, not
        # `[]` and not absent. Reading it as unknown would silence the check on
        # exactly the case it exists for.
        slice_ = endpointslice("api", ports=[])
        slice_["ports"] = None
        self.assertEqual(len(self.hits(self.svc(), slice_)), 1)

    def test_a_port_entry_with_no_number_does_not_resolve_it(self):
        slice_ = endpointslice("api", ports=["web"])
        slice_["ports"][0]["port"] = None
        self.assertEqual(len(self.hits(self.svc(), slice_)), 1)

    def test_an_unnamed_service_port_matches_the_unnamed_slice_entry(self):
        # The single-port shape: neither side carries a name, and an absent
        # name must not read as a different name from an empty one.
        svc = self.svc(port_name=None)
        del svc["spec"]["ports"][0]["name"]
        self.assertEqual(self.hits(svc, endpointslice("api", ports=[""])), [])
        self.assertEqual(len(self.hits(svc, endpointslice("api", ports=[]))), 1)

    def test_only_the_broken_port_of_a_multi_port_service_is_named(self):
        svc = service(
            "api",
            selector={"app": "api"},
            ports=[
                {"name": "web", "port": 80, "targetPort": "http"},
                {"name": "grpc", "port": 90, "targetPort": "grpc"},
            ],
        )
        hits = self.hits(svc, endpointslice("api", ports=["grpc"]), self.backend("grpc"))
        self.assertEqual(len(hits), 1)
        self.assertIn("'http'", hits[0]["excerpt"])
        self.assertNotIn("wants targetPort 'grpc'", hits[0]["excerpt"])

    def test_the_excerpt_names_what_the_workload_declares(self):
        # This is the whole remediation: the reader is choosing between the
        # names actually on the pod, and sending them to `kubectl` to find
        # them is what makes a finding unactionable.
        excerpt = self.hits(self.svc(), endpointslice("api", ports=[]), self.backend("grpc", "web"))[
            0
        ]["excerpt"]
        self.assertIn("Deployment/api", excerpt)
        self.assertIn("grpc, web", excerpt)

    def test_a_backend_declaring_no_ports_says_so(self):
        dep = deployment("api")
        dep["spec"]["template"]["metadata"] = {"labels": {"app": "api"}}
        excerpt = self.hits(self.svc(), endpointslice("api", ports=[]), dep)[0]["excerpt"]
        self.assertIn("no ports at all", excerpt)

    def test_an_unnamed_container_port_is_offered_as_its_number(self):
        # `targetPort: 8080` is as valid an answer as `targetPort: http`, and a
        # reader shown only the named ones would think there were none.
        dep = deployment("api")
        dep["spec"]["template"]["metadata"] = {"labels": {"app": "api"}}
        dep["spec"]["template"]["spec"]["containers"][0]["ports"] = [{"containerPort": 8080}]
        self.assertIn("8080", self.hits(self.svc(), endpointslice("api", ports=[]), dep)[0]["excerpt"])

    def test_a_native_sidecars_ports_are_offered_too(self):
        # An `initContainers` entry with `restartPolicy: Always` serves ports
        # like a regular container, so its names are valid answers.
        dep = deployment("api")
        dep["spec"]["template"]["metadata"] = {"labels": {"app": "api"}}
        dep["spec"]["template"]["spec"]["initContainers"] = [
            {"name": "proxy", "restartPolicy": "Always", "ports": [{"name": "proxy", "containerPort": 8643}]},
            {"name": "migrate", "ports": [{"name": "never", "containerPort": 9}]},
        ]
        excerpt = self.hits(self.svc(), endpointslice("api", ports=[]), dep)[0]["excerpt"]
        self.assertIn("proxy", excerpt)
        self.assertNotIn("never", excerpt)

    def test_no_backend_found_still_produces_a_finding(self):
        excerpt = self.hits(self.svc(), endpointslice("api", ports=[]))[0]["excerpt"]
        self.assertIn("the workload behind it", excerpt)

    def test_an_externally_published_service_is_critical(self):
        for svc_type in ("LoadBalancer", "NodePort"):
            with self.subTest(svc_type=svc_type):
                hits = self.hits(self.svc(svc_type=svc_type), endpointslice("api", ports=[]))
                self.assertEqual(hits[0]["severity"], "critical")

    def test_a_clusterip_service_is_major(self):
        self.assertEqual(
            self.hits(self.svc(), endpointslice("api", ports=[]))[0]["severity"], "major"
        )

    def test_the_standard_exclusions_apply(self):
        self.assertEqual(
            self.hits(self.svc(ns="kube-system"), endpointslice("api", ns="kube-system", ports=[])),
            [],
        )
        exempt = self.svc()
        exempt["metadata"]["labels"] = {collect.OPT_OUT_KEY: "exempt"}
        self.assertEqual(self.hits(exempt, endpointslice("api", ports=[])), [])

    def test_the_finding_names_what_reconciles_the_service(self):
        helm = self.svc()
        helm["metadata"]["annotations"] = {
            "meta.helm.sh/release-name": "kube-agents",
            "meta.helm.sh/release-namespace": "kubeagents-system",
        }
        self.assertIn("kube-agents", self.hits(helm, endpointslice("api", ports=[]))[0]["reconciler"])

    def test_one_service_never_carries_both_this_and_3_16(self):
        # 3.16 needs no ready endpoints and this needs some, so the two are
        # mutually exclusive on one Service by construction. This is the test
        # that keeps them that way, because a reader handed both findings on
        # one object would have no way to tell which one to act on.
        for slices in ([], [endpointslice("api", addresses=0, ports=[])], [endpointslice("api", ports=[])]):
            with self.subTest(slices=len(slices)):
                items = [self.svc(), *slices]
                orphaned = collect.check_service_selects_nothing(context_of(dump_of(*items)))
                self.assertEqual(len(orphaned) + len(self.hits(*items)), 1)


def fake_run(replies, calls):
    def run(argv, **kwargs):
        calls.append(argv)
        for key, result in replies.items():
            if key in argv:
                return result
        return Run(argv, 0, "", "", 0.01)

    return run


class TestFetchCredentials(unittest.TestCase):
    def test_sets_kubeconfig_per_cluster_not_export(self):
        calls = []
        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)):
                kc, result = collect.fetch_credentials(
                    "proj", "prod-usc1", "us-central1", run=fake_run({}, calls)
                )
        self.assertEqual(result.rc, 0)
        self.assertIn("prod-usc1", str(kc))
        self.assertIn("proj", str(kc))

    def test_a_failed_get_credentials_is_reported_not_raised(self):
        calls = []
        replies = {"get-credentials": Run([], 1, "", "cluster not found", 0.1)}
        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)):
                _, result = collect.fetch_credentials(
                    "proj", "gone", "us-central1", run=fake_run(replies, calls)
                )
        self.assertEqual(result.rc, 1)


class TestDumpStateGate(unittest.TestCase):
    def run_dump(self, stdout, rc=0):
        def run(argv, **kwargs):
            return Run(argv, rc, stdout, "", 0.05)

        with TemporaryDirectory() as tmp:
            with patch.object(collect, "SCRATCH_DIR", tmp):
                return collect.dump_state(Path(tmp) / "kc.yaml", "c1", run=run)

    def test_a_well_formed_dump_passes_the_gate(self):
        _, _, gate_ok = self.run_dump(json.dumps({"items": []}))
        self.assertTrue(gate_ok)

    def test_a_zero_byte_dump_fails_the_gate(self):
        _, _, gate_ok = self.run_dump("")
        self.assertFalse(gate_ok)

    def test_a_truncated_dump_fails_the_gate(self):
        # What a proxy truncation looks like: valid JSON up to a point,
        # then cut off mid-object.
        _, _, gate_ok = self.run_dump('{"items": [{"kind": "Depl')
        self.assertFalse(gate_ok)

    def test_a_non_zero_exit_fails_the_gate_even_with_output(self):
        _, _, gate_ok = self.run_dump(json.dumps({"items": []}), rc=1)
        self.assertFalse(gate_ok)

    def test_the_wrong_shape_fails_the_gate(self):
        # Valid JSON, but not a List -- e.g. an error object kubectl printed.
        _, _, gate_ok = self.run_dump(json.dumps({"error": "no"}))
        self.assertFalse(gate_ok)

    def test_two_same_named_clusters_in_two_projects_write_two_files(self):
        """The design's thread-safety rule is that a worker writes only to
        paths keyed by its own cluster, and named this file as the case no two
        threads can collide on. A cluster name is unique within a project, not
        across the eight this collector runs at once: two clusters called
        `prod` wrote one path, and the loser re-read the winner's dump — one
        cluster's workloads published under the other's name."""

        def run(argv, **kwargs):
            return Run(argv, 0, json.dumps({"items": []}), "", 0.05)

        with TemporaryDirectory() as tmp:
            with patch.object(collect, "SCRATCH_DIR", tmp):
                a, _, _ = collect.dump_state(Path(tmp) / "kc.yaml", "prod", project="acme-a", location="us-central1", run=run)
                b, _, _ = collect.dump_state(Path(tmp) / "kc.yaml", "prod", project="acme-b", location="us-central1", run=run)
        self.assertNotEqual(a, b)


class TestCollectCluster(unittest.TestCase):
    CLUSTER = {"name": "prod-usc1", "project": "acme", "location": "us-central1", "autopilot": False}

    def collect(self, dump_items, cred_rc=0, dump_rc=0, checks=None, cluster=None):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            if "get-credentials" in argv:
                return Run(argv, cred_rc, "", "" if cred_rc == 0 else "denied", 0.1)
            if argv[:2] == ["kubectl", "get"]:
                return Run(argv, dump_rc, json.dumps(dump_of(*dump_items)), "", 0.2)
            return Run(argv, 0, "", "", 0.01)

        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), \
                    patch.object(collect, "SCRATCH_DIR", tmp):
                result = collect.collect_cluster(
                    cluster or self.CLUSTER, "obtainability-audit",
                    checks or collect.OBTAINABILITY_CHECKS, run=run
                )
        return result, calls

    def test_a_clean_cluster_collects_with_no_candidates(self):
        # Scoped to the first two checks deliberately -- this test is about
        # no-requests/no-memory-limit interaction, not about every check in
        # the roster being individually satisfied by one fixture workload.
        clean = with_container_resources(
            deployment("api"),
            {"requests": {"cpu": "1", "memory": "1Gi"}, "limits": {"memory": "1Gi"}},
        )
        result, _ = self.collect([clean], checks=collect.OBTAINABILITY_CHECKS[:2])
        self.assertEqual(result["outcome"], "collected")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(len(result["commands"]), 2)  # one per check, same collection command

    def test_a_dirty_cluster_reports_both_checks(self):
        d = deployment("api")  # no resources at all
        result, _ = self.collect([d], checks=collect.OBTAINABILITY_CHECKS[:2])
        slugs = {c["check"] for c in result["candidates"]}
        self.assertEqual(slugs, {"no-requests", "no-memory-limit"})

    def test_the_full_roster_runs_together(self):
        # A bare two-replica Deployment with no resources, no PDB, no probes,
        # and no spreading should trip every workload-scoped check the
        # roster carries (all but the cluster-scoped ones, which need a
        # matching PDB/HPA to fire at all).
        d = deployment("api", **{"spec.replicas": 2})
        result, _ = self.collect([d])
        slugs = {c["check"] for c in result["candidates"]}
        self.assertEqual(
            slugs,
            {
                "no-requests", "no-memory-limit", "no-pdb", "no-spread",
                "probes-liveness",
            },
        )

    def test_a_fully_compliant_workload_trips_nothing_in_the_full_roster(self):
        d = with_container_resources(
            deployment("api", **{"spec.replicas": 1}),
            {"requests": {"cpu": "1", "memory": "1Gi"}, "limits": {"memory": "1Gi"}},
        )
        d["spec"]["template"]["spec"]["containers"][0]["readinessProbe"] = {"httpGet": {"path": "/", "port": 80}}
        # §3.18: one handler serving both probes is fine, and is the shape most
        # charts ship -- but only with headroom on the liveness side. Copied
        # verbatim into both blocks, as this fixture used to be, the two
        # deadlines are equal and the readiness probe can never fire first.
        d["spec"]["template"]["spec"]["containers"][0]["livenessProbe"] = {
            "httpGet": {"path": "/", "port": 80},
            "failureThreshold": 30,
        }
        result, _ = self.collect([d])
        self.assertEqual(result["candidates"], [])

    def test_a_cluster_with_nothing_in_scope_declares_the_checks_inapplicable(self):
        """A check with no workloads to examine has cleared nothing.

        12 of the 16 clusters the 2026-09-05 obtainability run covered hold no
        workload outside a system namespace. The document said 11 checks on 16
        targets, nothing inapplicable, no limitation -- which reads as fourteen
        clean clusters. Twelve of them were never in scope.
        """
        result, _ = self.collect([deployment("kube-dns", ns="kube-system")])
        self.assertEqual(result["outcome"], "collected")
        self.assertEqual(result["candidates"], [])
        na = {e["check"]: e["reason"] for e in result["checks_not_applicable"]}
        workload_slugs = {s.slug for s in collect.OBTAINABILITY_CHECKS if s.kind == "workload"}
        # The three cluster-kind checks that grade against the workload set
        # examined nothing either: a PDB is only graded against the workload
        # it covers, an HPA's floor only against a Service-backed workload.
        anchored = {"blocking-pdb", "pdb-overlapping", "hpa-floors-at-one"}
        self.assertEqual(set(na), workload_slugs | anchored)
        # The cluster-scoped checks still ran: an HPA pointing at a workload
        # that no longer exists, a PDB nothing can satisfy, a CronJob that has
        # stopped succeeding or whose runs overlap, and a Service selecting no
        # pod or asking for a port name none of them declares are all still
        # true of a cluster whose *workload* set is empty. 3.12, 3.15, 3.16 and
        # 3.17 in particular are only ever reachable this way -- neither a
        # CronJob nor a Service is a workload, so a cluster that holds nothing
        # else still owes all four an answer.
        cluster_slugs = {s.slug for s in collect.OBTAINABILITY_CHECKS if s.kind != "workload"} - anchored
        self.assertEqual(
            cluster_slugs,
            {
                "hpa-cannot-scale",
                "schedule-never-succeeds",
                "cronjob-runs-overlap",
                "service-selects-nothing",
                "service-port-unresolved",
            },
        )
        self.assertFalse(cluster_slugs & set(na))
        # §6 reads `commands` as the list of checks that ran, so a slug cannot
        # be in both.
        self.assertEqual({c["check"] for c in result["commands"]}, cluster_slugs)
        self.assertTrue(all("examined nothing" in r for r in na.values()))

    def test_a_cluster_with_one_workload_in_scope_declares_nothing_inapplicable(self):
        # The control for the test above: one auditable workload beside the
        # system-namespace noise and every check is back to having run.
        result, _ = self.collect([
            deployment("kube-dns", ns="kube-system"),
            with_container_resources(
                deployment("api"),
                {"requests": {"cpu": "1", "memory": "1Gi"}, "limits": {"memory": "1Gi"}},
            ),
        ])
        self.assertNotIn("checks_not_applicable", result)
        self.assertEqual(
            {c["check"] for c in result["commands"]},
            {s.slug for s in collect.OBTAINABILITY_CHECKS},
        )

    def test_get_credentials_failure_is_unreachable_not_a_shorter_list(self):
        result, calls = self.collect([deployment("api")], cred_rc=1)
        self.assertEqual(result["outcome"], "unreachable")
        self.assertNotIn("candidates", result)
        # The gate must never have been reached -- no kubectl call at all.
        self.assertFalse(any(c[:2] == ["kubectl", "get"] for c in calls))

    def test_a_failed_dump_is_gate_failed_not_a_shorter_list(self):
        result, _ = self.collect([deployment("api")], dump_rc=1)
        self.assertEqual(result["outcome"], "gate-failed")
        self.assertNotIn("candidates", result)

    def test_every_outcome_publishes_the_mode(self):
        # Six SOPs branch on Autopilot and one keys its cohorts on it, so a
        # manifest that withholds the mode sends the model back to
        # `clusters list` for a fact the collector already computed. It rides
        # on every shape, not just `collected`: a mode is a property of the
        # cluster, not of whether this run managed to read inside it.
        for kwargs, outcome in (
            ({}, "collected"),
            ({"cred_rc": 1}, "unreachable"),
            ({"dump_rc": 1}, "gate-failed"),
        ):
            for mode in (True, False):
                with self.subTest(outcome=outcome, autopilot=mode):
                    result, _ = self.collect(
                        [deployment("api")],
                        cluster={**self.CLUSTER, "autopilot": mode},
                        **kwargs,
                    )
                    self.assertEqual(result["outcome"], outcome)
                    self.assertIs(result["autopilot"], mode)

    def test_a_cluster_that_never_ran_still_publishes_the_mode(self):
        entry = collect.not_running_entry(
            {"name": "dr-west", "location": "us-west1", "status": "DEGRADED",
             "autopilot": {"enabled": True}},
            "acme",
        )
        self.assertEqual(entry["outcome"], "unreachable")
        self.assertIs(entry["autopilot"], True)
        # Absent `autopilot` in the gcloud payload means Standard, not unknown.
        self.assertIs(
            collect.not_running_entry({"name": "c", "status": "STOPPING"}, "acme")["autopilot"],
            False,
        )

    def test_a_reconciling_cluster_is_enumerated_rather_than_recorded(self):
        """`RECONCILING` means work is in progress on a cluster whose API
        server stays up, and any config change causes one -- so skipping it
        dropped the cluster from the audit for the duration of a routine edit.
        `PROVISIONING` has no API server yet and stays recorded."""
        listing = json.dumps(
            [
                {"name": "busy", "location": "us-east4", "status": "RECONCILING"},
                {"name": "new", "location": "us-west1", "status": "PROVISIONING"},
            ]
        )
        clusters, not_running, error = collect.enumerate_clusters(
            "acme", run=lambda argv, **kw: collect.Run(argv, 0, listing, "", 0.0)
        )
        self.assertEqual([c["name"] for c in clusters], ["busy"])
        self.assertEqual([c["target"] for c in clusters], ["acme/us-east4/busy"])
        self.assertEqual([c["name"] for c in not_running], ["acme/us-west1/new"])
        self.assertIn("PROVISIONING", not_running[0]["error"])
        self.assertIsNone(error)

    def test_autopilot_downgrades_no_requests_and_no_memory_limit(self):
        cluster = {**self.CLUSTER, "autopilot": True}
        calls = []

        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return Run(argv, 0, "", "", 0.1)
            if argv[:2] == ["kubectl", "get"]:
                return Run(argv, 0, json.dumps(dump_of(deployment("api"))), "", 0.2)
            return Run(argv, 0, "", "", 0.01)

        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), \
                    patch.object(collect, "SCRATCH_DIR", tmp):
                result = collect.collect_cluster(cluster, "obtainability-audit", collect.OBTAINABILITY_CHECKS, run=run)
        by_slug = {c["check"]: c for c in result["candidates"]}
        self.assertEqual(by_slug["no-requests"]["severity"], "minor")
        # Autopilot sets limits equal to requests at admission, so an absent
        # memory limit is not the unbounded pod §3.2 grades as major.
        self.assertEqual(by_slug["no-memory-limit"]["severity"], "minor")
        # The downgrade rewrites `impact`, and the arm flag has to survive it:
        # an Autopilot candidate whose sentence lost the marker would let the
        # model's guess at the QoS class publish on exactly the clusters where
        # the platform, not the manifest, decides it.
        self.assertIs(by_slug["no-requests"]["impact_authoritative"], True)
        self.assertIn("Autopilot: severity downgraded", by_slug["no-requests"]["impact"])

    def test_only_a_check_whose_hit_wrote_its_own_impact_is_authoritative(self):
        """The flag `adopt_arm_impact` keys off, at the point it is set.

        `no-requests` picks one of several QoS sentences per hit, and
        `no-memory-limit` one of two eviction sentences, so which one fired is
        an observation the model cannot reliably infer from the excerpt.
        `no-pdb` means one thing, its `impact` is the spec constant, and the
        model's object-specific rewrite of a constant is usually the better
        sentence -- so it must stay unflagged.
        """
        result, _ = self.collect([deployment("api")])
        by_slug = {c["check"]: c for c in result["candidates"]}
        for slug in ("no-requests", "no-memory-limit"):
            with self.subTest(check=slug):
                self.assertIs(by_slug[slug]["impact_authoritative"], True)
        self.assertNotIn("impact_authoritative", by_slug["no-pdb"])
        self.assertEqual(
            by_slug["no-pdb"]["impact"],
            next(s for s in collect.OBTAINABILITY_CHECKS if s.slug == "no-pdb").impact,
        )

    def test_the_collection_command_is_the_same_across_every_check(self):
        result, _ = self.collect([deployment("api")])
        commands = {c["command"] for c in result["commands"]}
        self.assertEqual(len(commands), 1)
        self.assertIn("kubectl get", next(iter(commands)))


class TestCollectFleet(unittest.TestCase):
    def test_every_enumerated_cluster_gets_an_entry_even_under_parallelism(self):
        # One cluster fails get-credentials; the others succeed. All three
        # must appear in the manifest -- a background failure under the
        # thread pool must not vanish a cluster from the result.
        clusters_json = json.dumps(
            [
                {"name": "c1", "location": "us-central1", "status": "RUNNING"},
                {"name": "c2", "location": "us-central1", "status": "RUNNING"},
                {"name": "c3", "location": "us-central1", "status": "RUNNING"},
            ]
        )

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return Run(argv, 0, clusters_json, "", 0.05)
            if "get-credentials" in argv:
                if "c2" in argv:
                    return Run(argv, 1, "", "denied", 0.05)
                return Run(argv, 0, "", "", 0.05)
            if argv[:2] == ["kubectl", "get"]:
                return Run(argv, 0, json.dumps(dump_of()), "", 0.05)
            return Run(argv, 0, "", "", 0.01)

        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), \
                    patch.object(collect, "SCRATCH_DIR", tmp):
                manifest = collect.collect_fleet("obtainability-audit", "acme", run=run, max_workers=3)

        names = {c["name"]: c["outcome"] for c in manifest["clusters"]}
        self.assertEqual(
            names,
            {
                "acme/us-central1/c1": "collected",
                "acme/us-central1/c2": "unreachable",
                "acme/us-central1/c3": "collected",
                # `--project` skipped discovery, and the manifest says so.
                collect.UNENUMERATED_PROJECTS_TARGET: "gate-failed",
            },
        )

    def test_a_non_running_cluster_is_recorded_rather_than_audited(self):
        # No check runs against it -- a STOPPING cluster has no API server to
        # read. But it stays in the manifest as a target the document has to
        # account for: dropped entirely it reads exactly like a cluster that
        # does not exist, and the run publishes a fleet-wide verdict over a
        # fleet quietly one cluster short.
        clusters_json = json.dumps(
            [
                {"name": "c1", "location": "us-central1", "status": "RUNNING"},
                {"name": "stopping", "location": "us-central1", "status": "STOPPING"},
            ]
        )

        def run(argv, **kwargs):
            if "list" in argv:
                return Run(argv, 0, clusters_json, "", 0.05)
            if "get-credentials" in argv:
                return Run(argv, 0, "", "", 0.05)
            if argv[:2] == ["kubectl", "get"]:
                return Run(argv, 0, json.dumps(dump_of()), "", 0.05)
            return Run(argv, 0, "", "", 0.01)

        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), \
                    patch.object(collect, "SCRATCH_DIR", tmp):
                manifest = collect.collect_fleet("obtainability-audit", "acme", run=run)
        outcomes = {c["name"]: c["outcome"] for c in manifest["clusters"]}
        self.assertEqual(
            outcomes,
            {"acme/us-central1/c1": "collected", "acme/us-central1/stopping": "unreachable", collect.UNENUMERATED_PROJECTS_TARGET: "gate-failed"},
        )
        stopping = next(c for c in manifest["clusters"] if c["name"] == "acme/us-central1/stopping")
        self.assertIn("STOPPING", stopping["error"])
        self.assertNotIn("commands", stopping)

    def test_one_cluster_crashing_costs_that_cluster_and_no_other(self):
        """`future.result()` re-raises, and every SOP redirects this
        collector's stdout into the manifest — so an unmodelled exception on
        one cluster used to leave a zero-byte file and lose the whole fleet.
        Only `GateFailure` was modelled; a `TypeError` off an unexpected API
        shape was not."""
        clusters_json = json.dumps(
            [
                {"name": "c1", "location": "us-central1", "status": "RUNNING"},
                {"name": "boom", "location": "us-central1", "status": "RUNNING"},
            ]
        )

        def run(argv, **kwargs):
            if "list" in argv:
                return Run(argv, 0, clusters_json, "", 0.05)
            if "get-credentials" in argv:
                return Run(argv, 0, "", "", 0.05)
            if argv[:2] == ["kubectl", "get"]:
                if any("boom" in str(v) for v in kwargs.get("env", {}).values()):
                    raise TypeError("unsupported operand type(s) for /: 'str' and 'str'")
                return Run(argv, 0, json.dumps(dump_of()), "", 0.05)
            return Run(argv, 0, "", "", 0.01)

        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), \
                    patch.object(collect, "SCRATCH_DIR", tmp):
                manifest = collect.collect_fleet("obtainability-audit", "acme", run=run)

        outcomes = {c["name"]: c["outcome"] for c in manifest["clusters"]}
        self.assertEqual(
            outcomes,
            {"acme/us-central1/c1": "collected", "acme/us-central1/boom": "gate-failed", collect.UNENUMERATED_PROJECTS_TARGET: "gate-failed"},
        )
        boom = next(c for c in manifest["clusters"] if c["name"] == "acme/us-central1/boom")
        self.assertIn("TypeError", boom["error"])

    def test_an_unknown_audit_id_refuses_rather_than_collecting_nothing(self):
        with self.assertRaises(ValueError):
            collect.collect_fleet("no-such-stream", "acme")

    def test_a_failed_listing_is_a_gate_failed_project_and_a_top_level_error(self):
        """An audit that could not list its one project read nothing, and the
        top-level `error` is what stops the SOP publishing an all-clear."""
        def run(argv, **kwargs):
            return Run(argv, 1, "", "permission denied", 0.05)

        manifest = collect.collect_fleet("obtainability-audit", "acme", run=run)
        by_name = {c["name"]: c for c in manifest["clusters"]}
        self.assertEqual(by_name["project/acme"]["outcome"], "gate-failed")
        self.assertIn("permission denied", by_name["project/acme"]["error"])
        self.assertIn("no cluster could be read", manifest["error"])

    def test_main_exits_nonzero_when_the_manifest_carries_an_error(self):
        """The manifest is still printed -- the shell has already redirected
        stdout into the file -- and the exit code says the run failed."""
        failed = {"clusters": [], "error": "project discovery failed"}
        with patch.object(collect, "collect_fleet", return_value=failed), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(collect.main(["obtainability-audit"]), 1)
        self.assertEqual(json.loads(out.getvalue()), failed)

    def test_main_names_the_candidate_count_on_stderr(self):
        """stdout is the manifest file; stderr is what the agent's shell shows,
        and a run that read only the manifest's head never saw a candidate."""
        manifest = {"clusters": [
            {"name": "p/l/a", "outcome": "collected",
             "candidates": [{"check": "no-pdb"}, {"check": "service-selects-nothing"}]},
            {"name": "p/l/b", "outcome": "collected", "candidates": [{"check": "no-pdb"}]},
            {"name": "project/q", "outcome": "gate-failed"},
        ]}
        with patch.object(collect, "collect_fleet", return_value=manifest), \
                patch("sys.stdout", new_callable=io.StringIO), \
                patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(collect.main(["obtainability-audit"]), 0)
        line = err.getvalue()
        self.assertIn("2 cluster(s) collected, 1 other target(s); 3 candidate(s) to report", line)
        self.assertIn("no-pdb: 2; service-selects-nothing: 1", line)
        self.assertIn("clusters[].candidates", line)

    def test_an_empty_fleet_summary_is_the_count_alone(self):
        manifest = {"clusters": [{"name": "p/l/a", "outcome": "collected", "candidates": []}]}
        self.assertEqual(
            collect.summary_line(manifest),
            "1 cluster(s) collected, 0 other target(s); 0 candidate(s) to report",
        )


class TestDiscovery(unittest.TestCase):
    """The project scope every collector shares: `fleet_drift.discover_fleet`
    and `patch_readiness.discover_fleet` sweep the active project plus every
    project `gcloud projects list` returns, and so does this one."""

    @staticmethod
    def discovering(base="base", listing=(0, "base\nother\n"), clusters=None):
        clusters = clusters or {}

        def run(argv, **kwargs):
            if argv[:4] == ["gcloud", "config", "get-value", "project"]:
                return Run(argv, 0, f"{base}\n" if base else "", "", 0.0)
            if argv[:3] == ["gcloud", "projects", "list"]:
                rc, out = listing
                return Run(argv, rc, out, "" if rc == 0 else "PERMISSION_DENIED", 0.0)
            if argv[:4] == ["gcloud", "container", "clusters", "list"]:
                project = argv[argv.index("--project") + 1]
                if project in clusters:
                    return clusters[project](argv)
                listed = [{"name": f"{project}-c", "location": "us-central1", "status": "RUNNING"}]
                return Run(argv, 0, json.dumps(listed), "", 0.0)
            if "get-credentials" in argv:
                return Run(argv, 0, "", "", 0.0)
            if argv[:2] == ["kubectl", "get"]:
                return Run(argv, 0, json.dumps(dump_of()), "", 0.0)
            return Run(argv, 0, "", "", 0.0)

        return run

    def collect(self, run):
        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), patch.object(collect, "SCRATCH_DIR", tmp):
                return collect.collect_fleet("obtainability-audit", run=run)

    def test_every_listed_project_is_swept_and_named_by_its_qualified_target(self):
        manifest = self.collect(self.discovering())
        self.assertEqual(
            [c["name"] for c in manifest["clusters"]],
            ["base/us-central1/base-c", "other/us-central1/other-c"],
        )
        self.assertNotIn("error", manifest)

    def test_the_same_name_in_two_projects_is_two_targets(self):
        same = lambda argv: Run(  # noqa: E731
            argv, 0, json.dumps([{"name": "seeded-a", "location": "us-central1", "status": "RUNNING"}]), "", 0.0
        )
        manifest = self.collect(self.discovering(clusters={"base": same, "other": same}))
        names = [c["name"] for c in manifest["clusters"]]
        self.assertEqual(names, ["base/us-central1/seeded-a", "other/us-central1/seeded-a"])
        for entry in manifest["clusters"]:
            for candidate in entry["candidates"]:
                self.assertEqual(candidate["cluster"], entry["name"])

    def test_a_failed_project_listing_leaves_an_unenumerated_row(self):
        manifest = self.collect(self.discovering(listing=(1, "")))
        by_name = {c["name"]: c for c in manifest["clusters"]}
        row = by_name[collect.UNENUMERATED_PROJECTS_TARGET]
        self.assertEqual(row["outcome"], "gate-failed")
        self.assertIn("PERMISSION_DENIED", row["error"])
        self.assertEqual(by_name["base/us-central1/base-c"]["outcome"], "collected")
        self.assertNotIn("error", manifest)

    def test_a_filtered_project_listing_leaves_an_unenumerated_row(self):
        manifest = self.collect(self.discovering(listing=(0, "other\n")))
        by_name = {c["name"]: c for c in manifest["clusters"]}
        self.assertIn("filtered", by_name[collect.UNENUMERATED_PROJECTS_TARGET]["error"])
        self.assertIn("base/us-central1/base-c", by_name)
        self.assertIn("other/us-central1/other-c", by_name)

    def test_no_project_at_all_is_a_top_level_error(self):
        manifest = self.collect(self.discovering(base="", listing=(1, "")))
        self.assertIn("project discovery failed", manifest["error"])
        self.assertEqual(manifest["clusters"], [])

    def test_a_project_with_the_api_disabled_is_empty_rather_than_lost(self):
        disabled = lambda argv: Run(argv, 1, "", "ERROR: SERVICE_DISABLED: container.googleapis.com", 0.0)  # noqa: E731
        manifest = self.collect(self.discovering(clusters={"other": disabled}))
        self.assertEqual([c["name"] for c in manifest["clusters"]], ["base/us-central1/base-c"])
        self.assertNotIn("error", manifest)

    def test_a_denied_project_is_a_gate_failed_row(self):
        denied = lambda argv: Run(argv, 1, "", "PERMISSION_DENIED on other", 0.0)  # noqa: E731
        manifest = self.collect(self.discovering(clusters={"other": denied}))
        by_name = {c["name"]: c for c in manifest["clusters"]}
        self.assertEqual(by_name["project/other"]["outcome"], "gate-failed")
        self.assertNotIn("error", manifest)

    def test_a_listing_some_zones_did_not_answer_keeps_its_clusters_and_says_so(self):
        partial = lambda argv: Run(  # noqa: E731
            argv,
            0,
            json.dumps([{"name": "base-c", "location": "us-central1", "status": "RUNNING"}]),
            "WARNING: The following zones did not respond: us-east1-b.",
            0.0,
        )
        manifest = self.collect(self.discovering(clusters={"base": partial}))
        by_name = {c["name"]: c for c in manifest["clusters"]}
        self.assertEqual(by_name["base/us-central1/base-c"]["outcome"], "collected")
        self.assertIn("us-east1-b", by_name["project/base"]["error"])


# What the manifest calls `c1` in project `acme`, and a full discovery that
# finds only that project. A scoped `--project` run would add an unenumerated
# row, which owes the document a `scope.skipped` entry of its own.
QUALIFIED_C1 = "acme/us-central1/c1"


def discovery_of_acme(argv):
    if argv[:4] == ["gcloud", "config", "get-value", "project"] or argv[:3] == ["gcloud", "projects", "list"]:
        return Run(argv, 0, "acme\n", "", 0.0)
    return None


class TestManifestComposesWithAuditReport(unittest.TestCase):
    """collect.py and audit_report.py are developed and tested independently
    against a shared manifest contract
    (docs/designs/fleet-audit-collector-manifest.md). This proves the contract
    actually holds: a real manifest from `collect_fleet`, fed into
    `audit_report.cross_check_manifest` alongside the `checks_run` list an
    agent would honestly copy from it, passes — and a `checks_run` that
    invents a check the manifest never ran is still rejected.
    """

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
        global audit_report
        import audit_report  # noqa: F401, imported for its cross_check_manifest

    def build_manifest(self, dump_items):
        clusters_json = json.dumps([{"name": "c1", "location": "us-central1", "status": "RUNNING"}])

        def run(argv, **kwargs):
            if discovered := discovery_of_acme(argv):
                return discovered
            if "list" in argv and "clusters" in argv:
                return Run(argv, 0, clusters_json, "", 0.05)
            if "get-credentials" in argv:
                return Run(argv, 0, "", "", 0.05)
            if argv[:2] == ["kubectl", "get"]:
                return Run(argv, 0, json.dumps(dump_of(*dump_items)), "", 0.1)
            return Run(argv, 0, "", "", 0.01)

        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), \
                    patch.object(collect, "SCRATCH_DIR", tmp):
                return collect.collect_fleet("obtainability-audit", run=run)

    def test_the_full_roster_of_checks_run_verifies_against_the_real_manifest(self):
        manifest = self.build_manifest([deployment("api")])
        checks_run = [{"check": c.slug, "command": "x"} for c in collect.OBTAINABILITY_CHECKS]
        doc = {"audit": "obtainability-audit", "scope": {"clusters": [{"name": QUALIFIED_C1, "checks_run": checks_run}]}}
        audit_report.cross_check_manifest(doc, manifest)  # must not raise

    def test_a_check_the_agent_did_not_actually_run_is_still_caught(self):
        manifest = self.build_manifest([deployment("api")])
        doc = {
            "audit": "obtainability-audit",
            "scope": {"clusters": [{"name": QUALIFIED_C1, "checks_run": [{"check": "no-hpa", "command": "x"}]}]},
        }
        # no-hpa IS in the real manifest, so this passes -- the negative case:
        audit_report.cross_check_manifest(doc, manifest)
        doc["scope"]["clusters"][0]["checks_run"].append({"check": "single-replica", "command": "x"})
        # single-replica is also real; still passes. Now fabricate one that
        # is not a rostered obtainability-audit slug at all -- the harness's
        # own roster validation (not this function) would catch that in
        # practice, but cross_check_manifest itself must refuse a slug the
        # manifest's commands never recorded, whatever it's called.
        doc["scope"]["clusters"][0]["checks_run"].append({"check": "not-a-real-check", "command": "x"})
        with self.assertRaises(audit_report.ValidationError):
            audit_report.cross_check_manifest(doc, manifest)

    def test_compliance_audits_full_roster_also_verifies_through_collect_fleet(self):
        # The multi-source builder is the part obtainability's version of
        # this test cannot cover -- five distinct collection commands,
        # cross-referenced, assembled by collect_fleet's outer enumeration
        # and thread pool, not just one cluster's collect_cluster call.
        clusters_json = json.dumps([{"name": "c1", "location": "us-central1", "status": "RUNNING"}])

        def run(argv, **kwargs):
            if discovered := discovery_of_acme(argv):
                return discovered
            if "list" in argv and "clusters" in argv:
                return Run(argv, 0, clusters_json, "", 0.05)
            if "get-credentials" in argv:
                return Run(argv, 0, "", "", 0.05)
            if argv[:2] == ["kubectl", "get"] and argv[2] == collect.COMPLIANCE_DUMP_KINDS:
                # One in-scope workload: with none, every workload-scoped
                # check is declared inapplicable and `checks_run` below is
                # rightly rejected for naming checks that examined nothing.
                return Run(argv, 0, json.dumps(dump_of(compliance_pod("app"))), "", 0.1)
            if argv[:3] == ["kubectl", "get", collect.KCC_CATEGORY]:
                # One healthy Config Connector object. An empty list here is
                # not the same thing: it declares `kcc-object-wedged`
                # inapplicable, and the full roster this test asserts against
                # would then rightly be rejected for naming a check nothing ran.
                return Run(argv, 0, json.dumps(dump_of(kcc_object("ComputeFirewall", "allow-ssh"))), "", 0.1)
            if argv[:2] == ["kubectl", "get"]:
                return Run(argv, 0, json.dumps(dump_of()), "", 0.1)
            if argv[:3] == ["gcloud", "container", "clusters"]:
                return Run(argv, 0, json.dumps({"workloadIdentityConfig": {"workloadPool": "x"}}), "", 0.1)
            if argv[:3] == ["gcloud", "container", "node-pools"]:
                return Run(argv, 0, "[]", "", 0.1)
            return Run(argv, 0, "", "", 0.01)

        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), patch.object(collect, "SCRATCH_DIR", tmp):
                manifest = collect.collect_fleet("compliance-audit", run=run)

        checks_run = [{"check": c.slug, "command": "x"} for c in collect.COMPLIANCE_CHECKS]
        doc = {"audit": "compliance-audit", "scope": {"clusters": [{"name": QUALIFIED_C1, "checks_run": checks_run}]}}
        audit_report.cross_check_manifest(doc, manifest)  # must not raise

    def test_ai_security_audits_full_roster_also_verifies_through_collect_fleet(self):
        clusters_json = json.dumps([{"name": "c1", "location": "us-central1", "status": "RUNNING"}])

        def run(argv, **kwargs):
            if discovered := discovery_of_acme(argv):
                return discovered
            if "list" in argv and "clusters" in argv:
                return Run(argv, 0, clusters_json, "", 0.05)
            if "get-credentials" in argv:
                return Run(argv, 0, "", "", 0.05)
            if argv[:2] == ["kubectl", "get"]:
                return Run(argv, 0, json.dumps(dump_of()), "", 0.1)
            return Run(argv, 0, "", "", 0.01)

        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), patch.object(collect, "SCRATCH_DIR", tmp):
                manifest = collect.collect_fleet("ai-security-audit", run=run)

        checks_run = [{"check": c.slug, "command": "x"} for c in collect.AI_SECURITY_CHECKS]
        doc = {"audit": "ai-security-audit", "scope": {"clusters": [{"name": QUALIFIED_C1, "checks_run": checks_run}]}}
        audit_report.cross_check_manifest(doc, manifest)  # must not raise

    def test_every_slug_the_collector_declares_exists_in_the_sop_roster(self):
        """The third edge of the SOP/roster/collector triangle.

        `test_check_rosters_match_the_sops` over in `test_audit_report.py`
        binds the SOP headings to `AUDITS`; nothing bound the collector to
        either, so a slug renamed in `CHECK_TABLES` alone stayed green here and
        only surfaced as a coverage denominator that never reached 100%. The
        containment is one-way on purpose: the roster is allowed to hold checks
        the collector does not implement -- those are the ones the model still
        evaluates by hand -- but a slug the collector emits and the roster does
        not know is a check no run can ever be credited with.
        """
        for audit_id, specs in sorted(collect.CHECK_TABLES.items()):
            with self.subTest(audit=audit_id):
                self.assertIn(
                    audit_id,
                    audit_report.AUDITS,
                    f"collect.CHECK_TABLES has an audit {audit_id!r} that "
                    f"audit_report.AUDITS does not define",
                )
                roster = set(audit_report.AUDITS[audit_id].checks)
                unknown = sorted({spec.slug for spec in specs} - roster)
                self.assertEqual(
                    [],
                    unknown,
                    f"collect.CHECK_TABLES[{audit_id!r}] emits {unknown}, which "
                    f"{audit_report.audit_sop(audit_id)} does not define as a "
                    f"check",
                )


# --------------------------------------------------------------------------- #
# compliance-audit
# --------------------------------------------------------------------------- #


def compliance_pod(name, ns="default", **meta_overrides):
    """A bare Pod — compliance's dump includes these (unlike obtainability's,
    which reads templates only). `pod_spec_of()` gives a mutable reference
    into the container list for tests that need to set securityContext etc.
    """
    doc = {
        "kind": "Pod",
        "metadata": {"namespace": ns, "name": name, "labels": {}, "annotations": {}},
        "spec": {"containers": [{"name": "app", "securityContext": {}}]},
    }
    doc["metadata"].update(meta_overrides)
    return doc


def kcc_object(kind, name, ns="kubeagents-system", ready="True", reason="UpToDate", message="The resource is up to date"):
    """A Config Connector object as `kubectl get gcp -A -o json` returns it.

    `ready=None` omits the condition entirely, which is what KCC has written
    in the seconds between an apply and its first reconcile.
    """
    doc = {"kind": kind, "metadata": {"namespace": ns, "name": name}, "status": {}}
    if ready is not None:
        condition = {"type": "Ready", "status": ready}
        if reason is not None:
            condition["reason"] = reason
        if message is not None:
            condition["message"] = message
        doc["status"]["conditions"] = [condition]
    return doc


def compliance_workload(kind, name, ns="default"):
    """A Deployment/StatefulSet/DaemonSet/CronJob wrapping the same pod spec
    shape `compliance_pod` uses, nested at the depth compliance's
    `_pod_spec_of` expects for that kind."""
    pod_spec = {"containers": [{"name": "app", "securityContext": {}}]}
    if kind == "CronJob":
        spec = {"jobTemplate": {"spec": {"template": {"spec": pod_spec}}}}
    else:
        spec = {"template": {"spec": pod_spec}}
    return {"kind": kind, "metadata": {"namespace": ns, "name": name, "labels": {}, "annotations": {}}, "spec": spec}


def pod_spec_of(doc):
    return collect._pod_spec_of(doc)


def crb(name, subjects, role="cluster-admin"):
    return {"kind": "ClusterRoleBinding", "metadata": {"name": name}, "roleRef": {"kind": "ClusterRole", "name": role}, "subjects": subjects}


def subject(kind, name, ns=None):
    d = {"kind": kind, "name": name}
    if ns is not None:
        d["namespace"] = ns
    return d


def cluster_role(name, rules, ns=None, labels=None):
    doc = {"kind": "ClusterRole" if ns is None else "Role", "metadata": {"name": name, "labels": labels or {}}, "rules": rules}
    if ns is not None:
        doc["metadata"]["namespace"] = ns
    return doc


def role_binding(role_kind, role_name, subjects, ns=None):
    doc = {
        "kind": "ClusterRoleBinding" if ns is None else "RoleBinding",
        "metadata": {"name": f"{role_name}-binding"},
        "roleRef": {"kind": role_kind, "name": role_name},
        "subjects": subjects,
    }
    if ns is not None:
        doc["metadata"]["namespace"] = ns
    return doc


def netpol(name, ns="default", pod_selector=None, ingress=None, policy_types=None):
    spec = {"podSelector": pod_selector if pod_selector is not None else {}}
    if ingress is not None:
        spec["ingress"] = ingress
    if policy_types is not None:
        spec["policyTypes"] = policy_types
    return {"kind": "NetworkPolicy", "metadata": {"namespace": ns, "name": name}, "spec": spec}


def namespace(name, labels=None):
    return {"kind": "Namespace", "metadata": {"name": name, "labels": labels or {}}}


def netpol_pod(name, ns="default", labels=None, phase="Running", owners=None):
    """A `context["pods"]` entry -- the pod-label view §2.6 needs to ask which
    pods a namespace's policies actually select."""
    return {"ns": ns, "name": name, "labels": labels or {}, "phase": phase, "owners": owners or []}


def owner(kind, name):
    return {"kind": kind, "name": name}


def ccnp(name, selector=None, ingress=None):
    """A Dataplane V2 ClusterNetworkPolicy. Defaults to the shape that
    suppresses §2.6 everywhere: every endpoint, ingress-isolating."""
    spec = {"endpointSelector": selector if selector is not None else {}, "ingress": ingress if ingress is not None else []}
    if not spec["ingress"]:
        spec["ingress"] = [{"fromEndpoints": [{"matchLabels": {"k8s:io.kubernetes.pod.namespace": "kube-system"}}]}]
    return {"kind": "ClusterNetworkPolicy", "metadata": {"name": name}, "spec": spec}


def default_sa(ns, automount=None):
    doc = {"kind": "ServiceAccount", "metadata": {"namespace": ns, "name": "default"}}
    if automount is not None:
        doc["automountServiceAccountToken"] = automount
    return doc


class TestComplianceNormalize(unittest.TestCase):
    def test_a_bare_unowned_pod_is_included(self):
        out = collect.normalize_compliance_workloads(dump_of(compliance_pod("standalone")))
        self.assertEqual(len(out), 1)

    def test_an_owned_pod_is_excluded_audit_the_controller_instead(self):
        pod = compliance_pod("api-abc123")
        pod["metadata"]["ownerReferences"] = [{"kind": "ReplicaSet", "name": "api"}]
        self.assertEqual(collect.normalize_compliance_workloads(dump_of(pod)), [])

    def test_a_deployment_template_is_included(self):
        out = collect.normalize_compliance_workloads(dump_of(compliance_workload("Deployment", "api")))
        self.assertEqual(len(out), 1)
        self.assertIn("containers", out[0]["spec"])

    def test_a_cronjob_resolves_two_levels_deep(self):
        out = collect.normalize_compliance_workloads(dump_of(compliance_workload("CronJob", "job")))
        self.assertEqual(out[0]["spec"]["containers"][0]["name"], "app")

    def test_a_suspended_cronjob_is_kept_and_marked(self):
        """Kept, because the template is what runs the moment someone resumes
        it; marked, because the impact a suspended CronJob's finding is written
        from is in the present tense."""
        c = compliance_workload("CronJob", "job")
        c["spec"]["suspend"] = True
        out = collect.normalize_compliance_workloads(dump_of(c))
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["suspended"])

    def test_an_unsuspended_cronjob_and_a_deployment_are_not_marked(self):
        for doc in (
            compliance_workload("CronJob", "job"),
            compliance_workload("Deployment", "api"),
            compliance_pod("standalone"),
        ):
            with self.subTest(kind=doc["kind"]):
                self.assertFalse(collect.normalize_compliance_workloads(dump_of(doc))[0]["suspended"])

    def test_suspend_false_reads_as_not_suspended(self):
        c = compliance_workload("CronJob", "job")
        c["spec"]["suspend"] = False
        self.assertFalse(collect.normalize_compliance_workloads(dump_of(c))[0]["suspended"])

    def test_a_scaled_to_zero_workload_is_kept_and_marked(self):
        """Suspension's counterpart for the kinds that cannot suspend: kept
        because the template is what runs on the next scale-up, marked because
        the impact is written in the present tense."""
        for kind in ("Deployment", "StatefulSet"):
            with self.subTest(kind=kind):
                d = compliance_workload(kind, "api")
                d["spec"]["replicas"] = 0
                out = collect.normalize_compliance_workloads(dump_of(d))
                self.assertEqual(len(out), 1)
                self.assertTrue(out[0]["scaled_to_zero"])

    def test_a_running_replica_count_is_not_marked(self):
        """Absent means one, not none — the commonest spec in the fleet omits
        `replicas` entirely, and reading that as zero would caveat everything."""
        for replicas in (1, 3, None):
            with self.subTest(replicas=replicas):
                d = compliance_workload("Deployment", "api")
                if replicas is None:
                    d["spec"].pop("replicas", None)
                else:
                    d["spec"]["replicas"] = replicas
                self.assertFalse(collect.normalize_compliance_workloads(dump_of(d))[0]["scaled_to_zero"])

    def test_kinds_without_a_replica_field_are_never_marked(self):
        """A DaemonSet's count is the node count and a bare Pod either exists or
        does not; neither has a `replicas: 0` to read, and a stray one on a
        DaemonSet spec is not a scale-to-zero."""
        ds = compliance_workload("DaemonSet", "agent")
        ds["spec"]["replicas"] = 0
        for doc in (ds, compliance_pod("standalone"), compliance_workload("CronJob", "job")):
            with self.subTest(kind=doc["kind"]):
                self.assertFalse(collect.normalize_compliance_workloads(dump_of(doc))[0]["scaled_to_zero"])

    def test_system_namespace_is_excluded(self):
        self.assertEqual(
            collect.normalize_compliance_workloads(dump_of(compliance_pod("x", ns="kube-system"))), []
        )

    def test_kubeagents_system_is_not_suppressed(self):
        # The harness audits itself -- unlike obtainability's suppression
        # list, compliance deliberately leaves this one unfiltered.
        out = collect.normalize_compliance_workloads(dump_of(compliance_pod("x", ns="kubeagents-system")))
        self.assertEqual(len(out), 1)

    def test_a_gke_addon_is_excluded(self):
        pod = compliance_pod("x")
        pod["metadata"]["labels"]["addonmanager.kubernetes.io/mode"] = "Reconcile"
        self.assertEqual(collect.normalize_compliance_workloads(dump_of(pod)), [])


class TestPrivilegedContainer(unittest.TestCase):
    def wl(self):
        return collect.normalize_compliance_workloads(dump_of(compliance_pod("x")))[0]

    def test_privileged_true_is_flagged(self):
        d = compliance_pod("x")
        pod_spec_of(d)["containers"][0]["securityContext"] = {"privileged": True}
        wl = collect.normalize_compliance_workloads(dump_of(d))[0]
        self.assertIsNotNone(collect.check_privileged_container(wl, context_of()))

    def test_sys_admin_capability_is_flagged(self):
        d = compliance_pod("x")
        pod_spec_of(d)["containers"][0]["securityContext"] = {"capabilities": {"add": ["SYS_ADMIN"]}}
        wl = collect.normalize_compliance_workloads(dump_of(d))[0]
        self.assertIsNotNone(collect.check_privileged_container(wl, context_of()))

    def test_allow_privilege_escalation_alone_is_never_flagged(self):
        d = compliance_pod("x")
        pod_spec_of(d)["containers"][0]["securityContext"] = {"allowPrivilegeEscalation": True}
        wl = collect.normalize_compliance_workloads(dump_of(d))[0]
        self.assertIsNone(collect.check_privileged_container(wl, context_of()))

    def test_a_plain_container_is_not_flagged(self):
        self.assertIsNone(collect.check_privileged_container(self.wl(), context_of()))


class TestHostNamespace(unittest.TestCase):
    def wl(self, **spec_overrides):
        d = compliance_pod("x")
        d["spec"].update(spec_overrides)
        return collect.normalize_compliance_workloads(dump_of(d))[0]

    def test_host_pid_is_critical(self):
        hit = collect.check_host_namespace(self.wl(hostPID=True), context_of())
        self.assertEqual(hit["severity"], "critical")

    def test_host_ipc_is_critical(self):
        hit = collect.check_host_namespace(self.wl(hostIPC=True), context_of())
        self.assertEqual(hit["severity"], "critical")

    def test_host_network_alone_is_major(self):
        hit = collect.check_host_namespace(self.wl(hostNetwork=True), context_of())
        self.assertEqual(hit["severity"], "major")

    def test_none_set_is_not_flagged(self):
        self.assertIsNone(collect.check_host_namespace(self.wl(), context_of()))

    def ds(self, host_network, host_port=None):
        doc = compliance_workload("DaemonSet", "cni-agent")
        spec = pod_spec_of(doc)
        spec["hostNetwork"] = host_network
        if host_port is not None:
            spec["containers"][0]["ports"] = [{"hostPort": host_port}]
        return collect.normalize_compliance_workloads(dump_of(doc))[0]

    def test_ingress_daemonset_with_hostnetwork_and_hostport_is_downgraded_to_minor(self):
        hit = collect.check_host_namespace(self.ds(True, host_port=443), context_of())
        self.assertEqual(hit["severity"], "minor")

    def test_daemonset_with_hostnetwork_but_no_hostport_stays_major(self):
        hit = collect.check_host_namespace(self.ds(True), context_of())
        self.assertEqual(hit["severity"], "major")

    def test_non_daemonset_with_hostnetwork_and_hostport_stays_major(self):
        hit = collect.check_host_namespace(self.wl(hostNetwork=True), context_of())
        self.assertEqual(hit["severity"], "major")

    def test_each_flag_publishes_only_its_own_clause(self):
        # The flag-when is an `or`, so a sentence naming all three namespaces
        # is false on every hit that sets one of them.
        for flag, clause, absent in (
            ("hostPID", collect._IMPACT_HOST_PID, collect._IMPACT_HOST_NETWORK),
            ("hostIPC", collect._IMPACT_HOST_IPC, collect._IMPACT_HOST_NETWORK),
            ("hostNetwork", collect._IMPACT_HOST_NETWORK, collect._IMPACT_HOST_PID),
        ):
            with self.subTest(flag=flag):
                hit = collect.check_host_namespace(self.wl(**{flag: True}), context_of())
                self.assertEqual(hit["impact"], clause)
                self.assertNotIn(absent, hit["impact"])

    def test_several_flags_compose_in_spec_order(self):
        hit = collect.check_host_namespace(self.wl(hostPID=True, hostIPC=True, hostNetwork=True), context_of())
        self.assertEqual(
            hit["impact"],
            f"{collect._IMPACT_HOST_PID} {collect._IMPACT_HOST_IPC} {collect._IMPACT_HOST_NETWORK}",
        )

    def test_the_hostnetwork_clause_never_says_policy_is_merely_bypassed(self):
        # "bypasses NetworkPolicy enforcement" reads as a policy that still
        # applies and is weaker; nothing applies, so a reader must not be sent
        # looking for a policy fix that cannot exist.
        self.assertNotIn("bypass", collect._IMPACT_HOST_NETWORK.lower())
        self.assertIn("out of NetworkPolicy", collect._IMPACT_HOST_NETWORK)

    def test_the_downgraded_daemonset_still_carries_the_hostnetwork_clause(self):
        hit = collect.check_host_namespace(self.ds(True, host_port=443), context_of())
        self.assertEqual(hit["impact"], collect._IMPACT_HOST_NETWORK)


class TestHostpathMount(unittest.TestCase):
    def wl(self, path, ro, mount_name="hostvol"):
        d = compliance_pod("x")
        d["spec"]["volumes"] = [{"name": mount_name, "hostPath": {"path": path}}]
        d["spec"]["containers"][0]["volumeMounts"] = [{"name": mount_name, "readOnly": ro}]
        return collect.normalize_compliance_workloads(dump_of(d))[0]

    def test_root_path_is_critical(self):
        hit = collect.check_hostpath_mount(self.wl("/", True), context_of())
        self.assertEqual(hit["severity"], "critical")

    def test_docker_socket_is_critical(self):
        hit = collect.check_hostpath_mount(self.wl("/var/run/docker.sock", True), context_of())
        self.assertEqual(hit["severity"], "critical")

    def test_a_writable_mount_is_critical_regardless_of_path(self):
        hit = collect.check_hostpath_mount(self.wl("/data", False), context_of())
        self.assertEqual(hit["severity"], "critical")

    def test_a_readonly_non_sensitive_path_is_major(self):
        hit = collect.check_hostpath_mount(self.wl("/data", True), context_of())
        self.assertEqual(hit["severity"], "major")

    def test_a_declared_but_unmounted_hostpath_is_never_flagged(self):
        d = compliance_pod("x")
        d["spec"]["volumes"] = [{"name": "v", "hostPath": {"path": "/"}}]
        wl = collect.normalize_compliance_workloads(dump_of(d))[0]
        self.assertIsNone(collect.check_hostpath_mount(wl, context_of()))

    def test_var_lib_kubelet_is_critical(self):
        hit = collect.check_hostpath_mount(self.wl("/var/lib/kubelet/pods", True), context_of())
        self.assertEqual(hit["severity"], "critical")


class TestClusterAdminBinding(unittest.TestCase):
    def test_a_non_system_service_account_is_critical(self):
        ctx = context_of(clusterrolebindings=[crb("b", [subject("ServiceAccount", "app", "default")])])
        hits = collect.check_cluster_admin_binding(ctx)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "critical")

    def test_a_system_masters_group_is_never_flagged(self):
        ctx = context_of(clusterrolebindings=[crb("b", [subject("Group", "system:masters")])])
        self.assertEqual(collect.check_cluster_admin_binding(ctx), [])

    def test_a_kube_system_service_account_is_never_flagged(self):
        ctx = context_of(clusterrolebindings=[crb("b", [subject("ServiceAccount", "x", "kube-system")])])
        self.assertEqual(collect.check_cluster_admin_binding(ctx), [])

    def test_a_google_managed_service_account_email_is_never_flagged(self):
        ctx = context_of(
            clusterrolebindings=[crb("b", [subject("User", "sa@my-project.iam.gserviceaccount.com")])]
        )
        self.assertEqual(collect.check_cluster_admin_binding(ctx), [])

    def test_an_org_email_group_is_downgraded_to_minor(self):
        ctx = context_of(clusterrolebindings=[crb("b", [subject("Group", "platform-admins@acme.com")])])
        hits = collect.check_cluster_admin_binding(ctx)
        self.assertEqual(hits[0]["severity"], "minor")

    def test_a_binding_to_a_different_role_is_never_flagged(self):
        ctx = context_of(
            clusterrolebindings=[crb("b", [subject("ServiceAccount", "app", "default")], role="edit")]
        )
        self.assertEqual(collect.check_cluster_admin_binding(ctx), [])

    def test_two_flagged_subjects_on_one_binding_are_one_candidate_naming_both(self):
        # One finding id per (check, object): a hit per subject would give
        # `finish` two candidates with one identity.
        ctx = context_of(
            clusterrolebindings=[
                crb("b", [subject("ServiceAccount", "a", "app"), subject("ServiceAccount", "b", "app")])
            ]
        )
        hits = collect.check_cluster_admin_binding(ctx)
        self.assertEqual(len(hits), 1)
        self.assertIn("ServiceAccount/app/a", hits[0]["excerpt"])
        self.assertIn("ServiceAccount/app/b", hits[0]["excerpt"])

    def test_the_worst_subject_on_a_binding_sets_its_severity(self):
        ctx = context_of(
            clusterrolebindings=[
                crb("b", [subject("Group", "platform-admins@acme.com"), subject("ServiceAccount", "a", "app")])
            ]
        )
        hits = collect.check_cluster_admin_binding(ctx)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "critical")


class TestWildcardRbac(unittest.TestCase):
    WILDCARD_RULE = [{"verbs": ["*"], "resources": ["*"], "apiGroups": ["*"]}]

    def test_a_bound_clusterrole_wildcard_is_critical(self):
        ctx = context_of(
            roles=[cluster_role("god-mode", self.WILDCARD_RULE)],
            clusterrolebindings=[role_binding("ClusterRole", "god-mode", [subject("ServiceAccount", "app", "default")])],
        )
        hits = collect.check_wildcard_rbac(ctx)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "critical")

    def test_a_bound_namespaced_role_wildcard_is_major(self):
        ctx = context_of(
            roles=[cluster_role("god-mode", self.WILDCARD_RULE, ns="default")],
            rolebindings=[role_binding("Role", "god-mode", [subject("ServiceAccount", "app", "default")], ns="default")],
        )
        hits = collect.check_wildcard_rbac(ctx)
        self.assertEqual(hits[0]["severity"], "major")

    def test_enumerated_verbs_over_a_wildcard_scope_are_still_an_escalation(self):
        """Spelling the verbs out is not a boundary, and the miss was live.

        `ClusterRole/argocd-server` on the reference fleet holds
        `apiGroups: ["*"], resources: ["*"], verbs: ["delete","get","patch"]`,
        bound to `ServiceAccount/argocd/argocd-server`, and graded clean because
        the predicate required a `*` in `verbs`. `get` on every resource in
        every group is every Secret in every namespace; `patch` on every
        resource rewrites a Deployment into a privileged pod.
        """
        ctx = context_of(
            roles=[cluster_role("argocd-server", [
                {"verbs": ["delete", "get", "patch"], "resources": ["*"], "apiGroups": ["*"]}
            ])],
            clusterrolebindings=[role_binding(
                "ClusterRole", "argocd-server",
                [subject("ServiceAccount", "argocd-server", "argocd")],
            )],
        )
        hits = collect.check_wildcard_rbac(ctx)
        self.assertEqual(len(hits), 1, hits)
        self.assertEqual(hits[0]["severity"], "critical")

    def test_a_read_only_wildcard_scope_is_left_alone(self):
        """The control, and the reason the new branch names its verbs.

        `apiGroups: ["*"], resources: ["*"], verbs: ["get","list","watch"]` is
        the ordinary cluster-monitoring shape -- a scraper, a backup agent --
        and grading every one of those critical is the false-positive flood this
        audit has already paid for once. Reading every Secret in the fleet is a
        real concern; it needs a check that can tell a scraper from an
        escalation, and this is not that check.
        """
        ctx = context_of(
            roles=[cluster_role("scraper", [
                {"verbs": ["get", "list", "watch"], "resources": ["*"], "apiGroups": ["*"]}
            ])],
            clusterrolebindings=[role_binding(
                "ClusterRole", "scraper",
                [subject("ServiceAccount", "prometheus", "monitoring")],
            )],
        )
        self.assertEqual(collect.check_wildcard_rbac(ctx), [])

    def test_enumerated_verbs_under_one_vendor_group_stay_suppressed(self):
        # The new branch requires `apiGroups == ["*"]`, so the operator-owns-its
        # -own-CRDs pattern keeps the exception it already had.
        ctx = context_of(
            roles=[cluster_role("cnrm-admin", [
                {"verbs": ["create", "patch"], "resources": ["*"], "apiGroups": ["cnrm.cloud.google.com"]}
            ])],
            clusterrolebindings=[role_binding(
                "ClusterRole", "cnrm-admin",
                [subject("ServiceAccount", "cnrm", "cnrm-system")],
            )],
        )
        self.assertEqual(collect.check_wildcard_rbac(ctx), [])

    def test_an_unbound_wildcard_role_is_never_flagged(self):
        ctx = context_of(roles=[cluster_role("god-mode", self.WILDCARD_RULE)])
        self.assertEqual(collect.check_wildcard_rbac(ctx), [])

    def test_a_bootstrapping_default_role_is_never_flagged(self):
        ctx = context_of(
            roles=[cluster_role("x", self.WILDCARD_RULE, labels={"kubernetes.io/bootstrapping": "rbac-defaults"})],
            clusterrolebindings=[role_binding("ClusterRole", "x", [subject("ServiceAccount", "app", "default")])],
        )
        self.assertEqual(collect.check_wildcard_rbac(ctx), [])

    def test_a_vendor_apigroup_wildcard_is_never_flagged(self):
        rule = [{"verbs": ["*"], "resources": ["*"], "apiGroups": ["kubeagents.io"]}]
        ctx = context_of(
            roles=[cluster_role("operator", rule)],
            clusterrolebindings=[role_binding("ClusterRole", "operator", [subject("ServiceAccount", "app", "default")])],
        )
        self.assertEqual(collect.check_wildcard_rbac(ctx), [])

    def test_a_core_group_wildcard_is_never_suppressed(self):
        rule = [{"verbs": ["*"], "resources": ["*"], "apiGroups": [""]}]
        ctx = context_of(
            roles=[cluster_role("core-god", rule)],
            clusterrolebindings=[role_binding("ClusterRole", "core-god", [subject("ServiceAccount", "app", "default")])],
        )
        self.assertEqual(len(collect.check_wildcard_rbac(ctx)), 1)

    # The live shape: GKE's `kubelet-api-admin`, bound to the API server's own
    # user so it can reach kubelets. Neither of the check's own two
    # suppressions sees it -- the label is not `rbac-defaults` and the name has
    # no `system:` prefix -- so before S2 it was one `critical` per cluster.
    GKE_ADDON = {"addonmanager.kubernetes.io/mode": "Reconcile"}

    def test_a_gke_managed_addon_role_is_never_flagged(self):
        ctx = context_of(
            roles=[cluster_role("kubelet-api-admin", self.WILDCARD_RULE, labels=self.GKE_ADDON)],
            clusterrolebindings=[
                role_binding("ClusterRole", "kubelet-api-admin", [subject("User", "kube-apiserver")])
            ],
        )
        self.assertEqual(collect.check_wildcard_rbac(ctx), [])

    def test_the_addon_suppression_keys_on_the_label_and_not_the_name(self):
        ctx = context_of(
            roles=[cluster_role("kubelet-api-admin", self.WILDCARD_RULE)],
            clusterrolebindings=[
                role_binding("ClusterRole", "kubelet-api-admin", [subject("User", "kube-apiserver")])
            ],
        )
        self.assertEqual(len(collect.check_wildcard_rbac(ctx)), 1)

    def bound(self, rules, subjects, name="role-under-test"):
        ctx = context_of(
            roles=[cluster_role(name, rules)],
            clusterrolebindings=[role_binding("ClusterRole", name, subjects)],
        )
        hits = collect.check_wildcard_rbac(ctx)
        self.assertEqual(len(hits), 1, hits)
        return hits[0]

    def test_a_verb_wildcard_publishes_the_unbounded_sentence(self):
        hit = self.bound(self.WILDCARD_RULE, [subject("ServiceAccount", "app", "default")])
        self.assertEqual(hit["impact"], collect._IMPACT_RBAC_ANY_VERB)

    def test_enumerated_verbs_do_not_publish_any_verb(self):
        """The live defect. `ClusterRole/argocd-server` holds `delete`, `get`,
        `patch` over every resource in every group, and the run published the
        wildcard sentence over it -- collapsing the direct/indirect distinction
        §2.5 spends a paragraph keeping. The verbs are parsed here, so the
        branch is arithmetic rather than a reading-comprehension task.
        """
        hit = self.bound(
            [{"verbs": ["delete", "get", "patch"], "resources": ["*"], "apiGroups": ["*"]}],
            [subject("ServiceAccount", "argocd-server", "argocd")],
        )
        self.assertNotIn("any verb on any resource", hit["impact"])
        self.assertIn("not a grant of every verb", hit["impact"])
        for clause in ("read every Secret", "rewrite an existing privileged workload", "destroy any object"):
            self.assertIn(clause, hit["impact"])
        self.assertNotIn("create privileged pods directly", hit["impact"])
        self.assertIn("real but indirect", hit["impact"])

    def test_delete_alone_keeps_its_qualifier_and_a_verb_list_drops_it(self):
        """§2.5 says `delete` *alone* is data loss rather than credential theft.
        Alone is the whole clause: appended beside `get`, it denies the
        credential theft the same sentence just described.
        """
        alone = self.bound(
            [{"verbs": ["delete"], "resources": ["*"], "apiGroups": ["*"]}],
            [subject("ServiceAccount", "reaper", "ops")],
        )
        self.assertIn("rather than credential theft", alone["impact"])
        beside = self.bound(
            [{"verbs": ["delete", "get"], "resources": ["*"], "apiGroups": ["*"]}],
            [subject("ServiceAccount", "reaper", "ops")],
        )
        self.assertIn("read every Secret", beside["impact"])
        self.assertNotIn("rather than credential theft", beside["impact"])

    def test_a_direct_escalation_verb_names_itself(self):
        for verb in collect._RBAC_DIRECT_VERBS:
            with self.subTest(verb=verb):
                hit = self.bound(
                    [{"verbs": [verb, "get"], "resources": ["*"], "apiGroups": ["*"]}],
                    [subject("ServiceAccount", "app", "default")],
                )
                self.assertIn(collect._IMPACT_RBAC_ANY_VERB, hit["impact"])
                self.assertIn(f"`{verb}` is the verb that reaches it", hit["impact"])

    def test_an_unrecognised_verb_list_understates_rather_than_overstates(self):
        """No hit reaches this branch today: stage 1 admits a rule only for a
        `*` verb or a member of `_ESCALATING_VERBS`, and every one of those is a
        direct verb or a clause family. Asserted at the function because the two
        lists are edited separately, and widening `_ESCALATING_VERBS` alone must
        not promote a new verb to the wildcard sentence by default.
        """
        unmatched = set(collect._ESCALATING_VERBS)
        for family, _ in collect._RBAC_VERB_CLAUSES:
            unmatched -= set(family)
        unmatched -= set(collect._RBAC_DIRECT_VERBS)
        self.assertEqual(unmatched, set(), "a stage-1 verb now falls to the defensive branch")
        impact = collect._wildcard_rbac_impact({"proxy"})
        self.assertNotIn("any verb on any resource", impact)
        self.assertIn("Read the verbs in the excerpt", impact)

    def test_the_excerpt_names_the_bound_principal(self):
        """The subject goes in the excerpt, not the recommendation, because
        `adopt_collector_evidence` restores the excerpt over whatever a run
        publishes. Live, the model invented one: `ClusterRole/argocd-server`'s
        finding told the operator to enumerate `argocd-application-controller`
        -- the other finding's subject, holding `verbs: ["*"]` on `["*"]`, so
        the mandated diff passes whatever the replacement says.
        """
        hit = self.bound(self.WILDCARD_RULE, [subject("ServiceAccount", "argocd-server", "argocd")])
        self.assertIn("; bound to system:serviceaccount:argocd:argocd-server", hit["excerpt"])
        # Still the matched rules verbatim in front, which §2.5 requires.
        self.assertTrue(hit["excerpt"].startswith(json.dumps(self.WILDCARD_RULE)))

    def test_a_user_and_a_group_are_spelled_by_name(self):
        hit = self.bound(
            self.WILDCARD_RULE,
            [subject("User", "dev@acme.com"), subject("Group", "platform-admins@acme.com")],
        )
        self.assertIn("; bound to dev@acme.com, platform-admins@acme.com", hit["excerpt"])

    def test_the_same_principal_bound_twice_is_listed_once(self):
        sa = subject("ServiceAccount", "app", "default")
        ctx = context_of(
            roles=[cluster_role("god-mode", self.WILDCARD_RULE)],
            clusterrolebindings=[
                role_binding("ClusterRole", "god-mode", [sa]),
                role_binding("ClusterRole", "god-mode", [sa]),
            ],
        )
        hits = collect.check_wildcard_rbac(ctx)
        self.assertEqual(hits[0]["excerpt"].count("system:serviceaccount:default:app"), 1)


class TestAnonymousRbacBinding(unittest.TestCase):
    WRITE_RULE = [{"verbs": ["get", "patch"], "resources": ["*"], "apiGroups": [""]}]
    READ_RULE = [{"verbs": ["get", "list", "watch"], "resources": ["*"], "apiGroups": [""]}]

    def test_anonymous_bound_to_cluster_admin_is_critical(self):
        ctx = context_of(clusterrolebindings=[crb("anon-admin", [subject("User", "system:anonymous")])])
        hits = collect.check_anonymous_rbac_binding(ctx)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "ClusterRoleBinding/anon-admin")
        self.assertIn("system:anonymous", hits[0]["excerpt"])

    def test_the_two_checks_it_complements_report_nothing_on_the_same_binding(self):
        """The gap this check exists for. §2.4 and §2.5 both run every subject
        through `_is_non_system_subject`, which excludes anything named
        `system:*` -- so `cluster-admin` bound to the internet is invisible to
        both of them. A future change that makes either one report it is what
        turns this check into a duplicate, and this is where that shows up."""
        ctx = context_of(
            roles=[cluster_role("cluster-admin", [{"verbs": ["*"], "resources": ["*"], "apiGroups": ["*"]}])],
            clusterrolebindings=[crb("anon-admin", [subject("User", "system:anonymous")])],
        )
        self.assertEqual(collect.check_cluster_admin_binding(ctx), [])
        self.assertEqual(collect.check_wildcard_rbac(ctx), [])
        self.assertEqual(len(collect.check_anonymous_rbac_binding(ctx)), 1)

    def test_unauthenticated_is_read_as_well_as_anonymous(self):
        ctx = context_of(clusterrolebindings=[crb("b", [subject("Group", "system:unauthenticated")])])
        self.assertEqual(len(collect.check_anonymous_rbac_binding(ctx)), 1)

    def test_a_read_only_role_is_still_flagged_for_an_anonymous_subject(self):
        """The arm split. `view` bound to `system:anonymous` hands every
        ConfigMap and pod spec in the cluster to an unauthenticated caller, so
        the role does not decide this arm."""
        ctx = context_of(
            roles=[cluster_role("view", self.READ_RULE)],
            clusterrolebindings=[
                role_binding("ClusterRole", "view", [subject("Group", "system:unauthenticated")])
            ],
        )
        self.assertEqual(len(collect.check_anonymous_rbac_binding(ctx)), 1)

    def test_an_anonymous_binding_to_an_absent_role_is_still_flagged(self):
        """No resolution happens on this arm, so a roleRef the dump does not
        carry does not silence it."""
        ctx = context_of(clusterrolebindings=[crb("b", [subject("User", "system:anonymous")], role="ghost")])
        self.assertEqual(len(collect.check_anonymous_rbac_binding(ctx)), 1)

    def test_authenticated_with_a_writing_role_is_flagged(self):
        ctx = context_of(
            roles=[cluster_role("edit", self.WRITE_RULE)],
            clusterrolebindings=[role_binding("ClusterRole", "edit", [subject("Group", "system:authenticated")])],
        )
        hits = collect.check_anonymous_rbac_binding(ctx)
        self.assertEqual(len(hits), 1)
        self.assertIn("grants patch", hits[0]["excerpt"])

    def test_authenticated_with_a_read_only_role_is_not_flagged(self):
        """`view` bound to `system:authenticated` is how many organisations
        make a cluster browsable, and `_sa_groups_granted` already treats
        it as a posture rather than a defect."""
        ctx = context_of(
            roles=[cluster_role("view", self.READ_RULE)],
            clusterrolebindings=[role_binding("ClusterRole", "view", [subject("Group", "system:authenticated")])],
        )
        self.assertEqual(collect.check_anonymous_rbac_binding(ctx), [])

    def test_authenticated_bound_to_an_absent_role_is_not_flagged(self):
        """A roleRef naming nothing grants nothing today, and there is no verb
        list to build the impact sentence from."""
        ctx = context_of(
            clusterrolebindings=[role_binding("ClusterRole", "ghost", [subject("Group", "system:authenticated")])]
        )
        self.assertEqual(collect.check_anonymous_rbac_binding(ctx), [])

    def test_a_verb_wildcard_counts_as_writing(self):
        ctx = context_of(
            roles=[cluster_role("god", [{"verbs": ["*"], "resources": ["*"], "apiGroups": ["*"]}])],
            clusterrolebindings=[role_binding("ClusterRole", "god", [subject("Group", "system:authenticated")])],
        )
        hits = collect.check_anonymous_rbac_binding(ctx)
        self.assertEqual(len(hits), 1)
        self.assertIn("grants *", hits[0]["excerpt"])

    def test_the_four_baseline_roles_are_never_flagged_for_authenticated_callers(self):
        for role in sorted(collect._BASELINE_AUTHENTICATED_ROLES):
            with self.subTest(role=role):
                ctx = context_of(
                    roles=[cluster_role(role, self.WRITE_RULE)],
                    clusterrolebindings=[
                        role_binding("ClusterRole", role, [subject("Group", "system:authenticated")])
                    ],
                )
                self.assertEqual(collect.check_anonymous_rbac_binding(ctx), [])

    def test_public_info_viewer_is_the_one_baseline_role_bound_to_anonymous_callers(self):
        ctx = context_of(
            clusterrolebindings=[
                role_binding("ClusterRole", "system:public-info-viewer", [subject("Group", "system:unauthenticated")])
            ]
        )
        self.assertEqual(collect.check_anonymous_rbac_binding(ctx), [])

    def test_re_enabling_anonymous_discovery_is_flagged(self):
        # Kubernetes 1.14 removed `system:unauthenticated` from these bindings;
        # putting it back is a choice someone made.
        for role in ("system:discovery", "system:basic-user", "system:service-account-issuer-discovery"):
            with self.subTest(role=role):
                ctx = context_of(
                    clusterrolebindings=[
                        role_binding("ClusterRole", role, [subject("Group", "system:unauthenticated")])
                    ]
                )
                hits = collect.check_anonymous_rbac_binding(ctx)
                self.assertEqual(len(hits), 1)
                self.assertIn("presented no credential", hits[0]["impact"])

    def test_service_account_groups_belong_to_the_other_check(self):
        for name in ("system:serviceaccounts", "system:serviceaccounts:default"):
            with self.subTest(name=name):
                ctx = context_of(clusterrolebindings=[crb("b", [subject("Group", name)])])
                self.assertEqual(collect.check_anonymous_rbac_binding(ctx), [])

    def test_an_ordinary_system_component_subject_is_not_flagged(self):
        ctx = context_of(clusterrolebindings=[crb("b", [subject("User", "system:kube-scheduler")])])
        self.assertEqual(collect.check_anonymous_rbac_binding(ctx), [])

    def test_a_namespaced_rolebinding_carries_its_namespace(self):
        ctx = context_of(
            rolebindings=[
                role_binding("Role", "local", [subject("User", "system:anonymous")], ns="payments")
            ]
        )
        hits = collect.check_anonymous_rbac_binding(ctx)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["namespace"], "payments")
        self.assertEqual(hits[0]["object"], "RoleBinding/local-binding")

    def test_a_namespaced_role_only_resolves_inside_its_own_namespace(self):
        """A `Role` of the same name in another namespace is a different
        object, and resolving against it would grade this binding on rules it
        does not have."""
        ctx = context_of(
            roles=[cluster_role("local", self.WRITE_RULE, ns="other")],
            rolebindings=[
                role_binding("Role", "local", [subject("Group", "system:authenticated")], ns="payments")
            ],
        )
        self.assertEqual(collect.check_anonymous_rbac_binding(ctx), [])

    def test_a_service_account_subject_is_not_this_checks_finding(self):
        ctx = context_of(clusterrolebindings=[crb("b", [subject("ServiceAccount", "system:anonymous", "default")])])
        self.assertEqual(collect.check_anonymous_rbac_binding(ctx), [])

    def test_one_binding_naming_both_universal_subjects_is_one_candidate_naming_both(self):
        # One finding id per (check, object): two candidates for one binding
        # would be refused by `finish`, or lose a subject from the excerpt.
        ctx = context_of(
            clusterrolebindings=[
                crb("b", [subject("User", "system:anonymous"), subject("Group", "system:unauthenticated")])
            ]
        )
        hits = collect.check_anonymous_rbac_binding(ctx)
        self.assertEqual(len(hits), 1)
        self.assertIn("User/system:anonymous", hits[0]["excerpt"])
        self.assertIn("Group/system:unauthenticated", hits[0]["excerpt"])

    def test_an_anonymous_subject_beside_a_writing_authenticated_one_takes_the_anonymous_impact(self):
        ctx = context_of(
            roles=[cluster_role("edit", self.WRITE_RULE)],
            clusterrolebindings=[
                role_binding(
                    "ClusterRole", "edit",
                    [subject("Group", "system:authenticated"), subject("User", "system:anonymous")],
                )
            ],
        )
        hits = collect.check_anonymous_rbac_binding(ctx)
        self.assertEqual(len(hits), 1)
        self.assertIn("presented no credential", hits[0]["impact"])
        self.assertIn("Group/system:authenticated", hits[0]["excerpt"])
        self.assertIn("grants", hits[0]["excerpt"])

    def test_the_two_arms_carry_different_impact_sentences(self):
        anon = collect.check_anonymous_rbac_binding(
            context_of(clusterrolebindings=[crb("a", [subject("User", "system:anonymous")])])
        )
        auth = collect.check_anonymous_rbac_binding(
            context_of(
                roles=[cluster_role("edit", self.WRITE_RULE)],
                clusterrolebindings=[role_binding("ClusterRole", "edit", [subject("Group", "system:authenticated")])],
            )
        )
        self.assertNotEqual(anon[0]["impact"], auth[0]["impact"])
        self.assertIn("presented no credential", anon[0]["impact"])
        self.assertIn("is not a team", auth[0]["impact"])

    def test_a_binding_with_no_subjects_reports_nothing(self):
        ctx = context_of(clusterrolebindings=[crb("b", [])])
        self.assertEqual(collect.check_anonymous_rbac_binding(ctx), [])


class TestPodWorkloadRef(unittest.TestCase):
    """The excerpt of a partial-coverage `netpol-missing` finding names the
    workloads no policy selects. That name is the only actionable string in
    the finding, so it has to be an object the reader can fetch."""

    DEPLOY = {"kind": "Deployment", "ns": "kubeagents-system", "name": "kube-agents-controller-manager"}

    def ref(self, pod, workloads=None):
        return collect._pod_workload_ref(pod, workloads if workloads is not None else [self.DEPLOY])

    def test_a_deployment_pod_resolves_through_its_replicaset(self):
        pod = netpol_pod(
            "kube-agents-controller-manager-674ffccc57-drmjx",
            ns="kubeagents-system",
            labels={"app.kubernetes.io/name": "kube-agents-operator"},
            owners=[owner("ReplicaSet", "kube-agents-controller-manager-674ffccc57")],
        )
        self.assertEqual(self.ref(pod), "Deployment/kube-agents-controller-manager")

    def test_the_label_value_is_not_used_even_when_it_is_the_only_name_present(self):
        """`app.kubernetes.io/name: kube-agents-operator` on this fleet names
        no object at all -- `kubectl get deploy kube-agents-operator` is
        `NotFound` -- and the 2026-09-05 compliance report published it as the
        uncovered workload in `kubeagents-system`."""
        pod = netpol_pod(
            "kube-agents-controller-manager-674ffccc57-drmjx",
            ns="kubeagents-system",
            labels={"app.kubernetes.io/name": "kube-agents-operator", "app": "operator", "k8s-app": "op"},
            owners=[owner("ReplicaSet", "kube-agents-controller-manager-674ffccc57")],
        )
        self.assertNotIn("kube-agents-operator", self.ref(pod))
        self.assertNotIn("k8s-app", self.ref(pod))

    def test_a_replicaset_whose_deployment_is_not_in_the_dump_stays_a_replicaset(self):
        # A bare ReplicaSet is a real object with that exact name. Guessing a
        # Deployment behind it would invent one.
        pod = netpol_pod("rs-abc123-x", ns="kubeagents-system", owners=[owner("ReplicaSet", "rs-abc123")])
        self.assertEqual(self.ref(pod), "ReplicaSet/rs-abc123")

    def test_a_cronjob_pod_resolves_two_levels_up_and_a_bare_job_does_not(self):
        cj = {"kind": "CronJob", "ns": "kubeagents-system", "name": "kube-agents-selfimprove"}
        pod = netpol_pod(
            "kube-agents-selfimprove-29810760-g8wtm",
            ns="kubeagents-system",
            owners=[owner("Job", "kube-agents-selfimprove-29810760")],
        )
        self.assertEqual(self.ref(pod, [cj]), "CronJob/kube-agents-selfimprove")
        self.assertEqual(self.ref(pod, []), "Job/kube-agents-selfimprove-29810760")

    def test_a_statefulset_or_daemonset_owner_is_used_as_is(self):
        for kind in ("StatefulSet", "DaemonSet"):
            with self.subTest(kind=kind):
                pod = netpol_pod("db-0", ns="kubeagents-system", owners=[owner(kind, "db")])
                self.assertEqual(self.ref(pod), f"{kind}/db")

    def test_an_unowned_pod_is_named_as_a_pod(self):
        # A bare pod is its own object: the name is exact and nothing recreates
        # it under a different one.
        pod = netpol_pod("debug-shell", ns="kubeagents-system", labels={"app": "debug"})
        self.assertEqual(self.ref(pod), "Pod/debug-shell")

    def test_the_controlling_owner_wins_over_the_others(self):
        pod = {
            "ns": "kubeagents-system", "name": "p-1", "labels": {}, "phase": "Running",
            "owners": [owner("StatefulSet", "sts-a")],
        }
        meta = {"ownerReferences": [
            {"kind": "ConfigMap", "name": "cm-a"},
            {"kind": "StatefulSet", "name": "sts-a", "controller": True},
        ]}
        pod["owners"] = collect._controller_refs(meta)
        self.assertEqual(self.ref(pod), "StatefulSet/sts-a")

    def test_an_owner_set_with_no_controller_still_names_something(self):
        meta = {"ownerReferences": [{"kind": "StatefulSet", "name": "sts-a"}]}
        self.assertEqual(collect._controller_refs(meta), [{"kind": "StatefulSet", "name": "sts-a"}])

    def test_a_same_named_workload_in_another_namespace_does_not_satisfy_the_lookup(self):
        elsewhere = {"kind": "Deployment", "ns": "other", "name": "api"}
        pod = netpol_pod("api-abc123-x", ns="kubeagents-system", owners=[owner("ReplicaSet", "api-abc123")])
        self.assertEqual(self.ref(pod, [elsewhere]), "ReplicaSet/api-abc123")


class TestNetpolMissing(unittest.TestCase):
    def test_zero_policies_with_workloads_is_major(self):
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[],
            workloads=[{"kind": "Pod", "ns": "payments", "name": "api"}],
        )
        hits = collect.check_netpol_missing(ctx)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "major")

    def test_the_excerpt_never_claims_the_cluster_has_no_policies(self):
        # kube-agents-host has eleven NetworkPolicies and none in cert-manager.
        # The excerpt is rendered under a cluster-wide `-A` command, so a bare
        # "zero NetworkPolicies" is a false statement beside a true finding.
        ctx = context_of(
            namespaces=[namespace("cert-manager"), namespace("argocd")],
            networkpolicies=[netpol("deny", ns="argocd", policy_types=["Ingress"])],
            workloads=[
                {"kind": "Pod", "ns": "cert-manager", "name": "webhook"},
                {"kind": "Pod", "ns": "argocd", "name": "server"},
            ],
        )
        hits = collect.check_netpol_missing(ctx)
        self.assertEqual([h["namespace"] for h in hits], ["cert-manager"])
        self.assertEqual(
            hits[0]["excerpt"],
            "no NetworkPolicy in this namespace; 1 in other namespaces of this cluster",
        )
        # The count carries no verdict: it tallies allow-all and
        # system-namespace policies too, so it cannot support a claim that
        # this namespace is the only gap.
        self.assertNotIn("the gap", hits[0]["excerpt"])

    def test_a_cluster_with_no_policies_at_all_says_only_the_namespace_part(self):
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[],
            workloads=[{"kind": "Pod", "ns": "payments", "name": "api"}],
        )
        self.assertEqual(collect.check_netpol_missing(ctx)[0]["excerpt"], "no NetworkPolicy in this namespace")

    def test_zero_policies_and_zero_workloads_is_not_flagged(self):
        ctx = context_of(namespaces=[namespace("empty")], networkpolicies=[], workloads=[])
        self.assertEqual(collect.check_netpol_missing(ctx), [])

    def test_an_allow_all_policy_is_minor(self):
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[netpol("allow-all", ns="payments", ingress=[{}])],
            workloads=[{"kind": "Pod", "ns": "payments", "name": "api"}],
        )
        hits = collect.check_netpol_missing(ctx)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "minor")
        self.assertIn("NetworkPolicy/allow-all", hits[0]["object"])

    def test_a_real_default_deny_policy_is_never_flagged(self):
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[netpol("deny", ns="payments", policy_types=["Ingress"])],
            workloads=[{"kind": "Pod", "ns": "payments", "name": "api"}],
        )
        self.assertEqual(collect.check_netpol_missing(ctx), [])

    def test_a_default_deny_with_no_policy_types_is_never_flagged(self):
        # `podSelector: {}` alone derives to `policyTypes: [Ingress]` with no
        # rule admitting anything: the deny-all §2.6's remediation writes.
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[netpol("deny", ns="payments")],
            workloads=[{"kind": "Pod", "ns": "payments", "name": "api"}],
        )
        self.assertEqual(collect.check_netpol_missing(ctx), [])

    def test_an_empty_ingress_rule_on_an_egress_only_policy_is_not_allow_all(self):
        # Kubernetes ignores `ingress` on a policy whose declared types omit it.
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[
                netpol("egress", ns="payments", ingress=[{}], policy_types=["Egress"]),
                netpol("deny", ns="payments", policy_types=["Ingress"]),
            ],
            workloads=[{"kind": "Pod", "ns": "payments", "name": "api"}],
        )
        self.assertEqual(collect.check_netpol_missing(ctx), [])

    def test_a_system_namespace_is_never_flagged(self):
        ctx = context_of(namespaces=[namespace("kube-system")], networkpolicies=[], workloads=[])
        self.assertEqual(collect.check_netpol_missing(ctx), [])

    def test_a_cluster_network_policy_suppresses_zero_policy_namespaces(self):
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[],
            workloads=[{"kind": "Pod", "ns": "payments", "name": "api"}],
            cluster_network_policies=[ccnp("fleet-wide")],
        )
        self.assertEqual(collect.check_netpol_missing(ctx), [])

    def test_a_narrow_cluster_policy_does_not_suppress_every_namespace(self):
        """The suppression was `bool(cluster_network_policies)`: one policy
        anywhere silenced §2.6 across the whole cluster. GKE ships Dataplane V2
        policies of its own, so a cluster could report no default-allow
        namespaces on the strength of a policy selecting one workload's
        labels."""
        ctx = context_of(
            namespaces=[namespace("payments"), namespace("shop")],
            networkpolicies=[],
            workloads=[{"kind": "Pod", "ns": "payments", "name": "api"}, {"kind": "Pod", "ns": "shop", "name": "web"}],
            cluster_network_policies=[ccnp("just-shop", selector={"matchLabels": {"k8s:io.kubernetes.pod.namespace": "shop"}})],
        )
        hits = collect.check_netpol_missing(ctx)
        self.assertEqual([h["namespace"] for h in hits], ["payments"])

    def test_a_cluster_policy_selecting_pod_labels_suppresses_nothing(self):
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[],
            workloads=[{"kind": "Pod", "ns": "payments", "name": "api"}],
            cluster_network_policies=[ccnp("by-app", selector={"matchLabels": {"app": "api"}})],
        )
        self.assertEqual(len(collect.check_netpol_missing(ctx)), 1)

    def test_an_egress_only_cluster_policy_suppresses_nothing(self):
        """§2.6 asks who can reach these pods. Cilium isolates ingress only for
        a policy carrying an `ingress` section, so an egress-only cluster
        policy leaves the namespace exactly as reachable as it was."""
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[],
            workloads=[{"kind": "Pod", "ns": "payments", "name": "api"}],
            cluster_network_policies=[{"kind": "ClusterNetworkPolicy", "metadata": {"name": "egress"}, "spec": {"endpointSelector": {}, "egress": [{}]}}],
        )
        self.assertEqual(len(collect.check_netpol_missing(ctx)), 1)

    def test_no_cluster_network_policy_still_flags_zero_policy_namespaces(self):
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[],
            workloads=[{"kind": "Pod", "ns": "payments", "name": "api"}],
            cluster_network_policies=[],
        )
        self.assertEqual(len(collect.check_netpol_missing(ctx)), 1)

    def test_a_namespace_whose_only_pods_are_controller_owned_is_still_flagged(self):
        """The defect this check spent every run not finding.

        `normalize_compliance_workloads` drops a Pod with `ownerReferences`, so
        a namespace running nothing but a Deployment's pods reaches the check
        with no Pod-kind workload at all. Reading exposure off that set made it
        "zero workloads, pure churn" and skipped the namespace -- which is the
        ordinary namespace, and the one §2.6 exists to report.
        """
        ctx = context_of(
            namespaces=[namespace("cert-manager")],
            networkpolicies=[],
            workloads=[{"kind": "Deployment", "ns": "cert-manager", "name": "cert-manager"}],
            pod_namespaces={"cert-manager"},
        )
        hits = collect.check_netpol_missing(ctx)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "major")

    def test_a_namespace_with_a_workload_but_no_running_pod_is_not_flagged(self):
        """The other side of it: §2.6's test is `get pods … | wc -l`, so a
        Deployment scaled to zero is not exposure. Counting workloads instead
        of pods would fix the case above by flagging this one."""
        ctx = context_of(
            namespaces=[namespace("dormant")],
            networkpolicies=[],
            workloads=[{"kind": "Deployment", "ns": "dormant", "name": "batch"}],
            pod_namespaces=set(),
        )
        self.assertEqual(collect.check_netpol_missing(ctx), [])

    def test_a_pod_no_policy_selects_is_flagged_even_where_policies_exist(self):
        """NetworkPolicy is additive and pod-scoped, so "this namespace has a
        policy" and "this pod has a policy" are different facts and only the
        second decides exposure. Deciding coverage per namespace let a pod
        selected by nothing sit behind a namespace that graded clean --
        `kubeagents-system` on the reference fleet, whose four policies name
        four workloads and leave the operator's manager pod reachable.
        """
        ctx = context_of(
            namespaces=[namespace("kubeagents-system")],
            networkpolicies=[netpol("litellm", ns="kubeagents-system", pod_selector={"matchLabels": {"app": "litellm"}}, policy_types=["Ingress"])],
            pods=[
                netpol_pod("litellm-7bcc-9rjvl", ns="kubeagents-system", labels={"app": "litellm"}),
                netpol_pod(
                    "kube-agents-controller-manager-764d-46gl4",
                    ns="kubeagents-system",
                    labels={"app.kubernetes.io/name": "kube-agents-operator"},
                    owners=[owner("ReplicaSet", "kube-agents-controller-manager-764d")],
                ),
            ],
            workloads=[{"kind": "Deployment", "ns": "kubeagents-system", "name": "kube-agents-controller-manager"}],
            pod_namespaces={"kubeagents-system"},
        )
        hits = collect.check_netpol_missing(ctx)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "major")
        self.assertEqual(hits[0]["object"], "Namespace/kubeagents-system")
        self.assertIn("1 of 2 pods", hits[0]["excerpt"])
        # The object a reader can `kubectl get`, not the label value the
        # 2026-09-05 report published in its place.
        self.assertIn("Deployment/kube-agents-controller-manager", hits[0]["excerpt"])
        self.assertNotIn("kube-agents-operator", hits[0]["excerpt"])

    def test_the_partial_arm_carries_its_own_impact_and_the_other_two_do_not(self):
        """§2.6 forbids arm 1's sentence on arm 3 by name -- "the other m - n
        pods are policed, and saying otherwise tells an operator their working
        policies do nothing" -- and the live run published it anyway, on a
        finding whose own title had taken the correct branch. So the collector
        writes arm 3's sentence and `adopt_arm_impact` restores it.

        Arms 1 and 2 deliberately stay on the table default: they share one true
        sentence, and leaving it unflagged is what lets a run name the actual
        namespace the way 39 of this install's 106 findings do.
        """
        partial = context_of(
            namespaces=[namespace("kubeagents-system")],
            networkpolicies=[netpol("litellm", ns="kubeagents-system", pod_selector={"matchLabels": {"app": "litellm"}}, policy_types=["Ingress"])],
            pods=[
                netpol_pod("litellm-7bcc-9rjvl", ns="kubeagents-system", labels={"app": "litellm"}),
                netpol_pod("manager-764d-46gl4", ns="kubeagents-system", labels={"app": "operator"}),
            ],
            pod_namespaces={"kubeagents-system"},
        )
        hit = collect.check_netpol_missing(partial)[0]
        self.assertEqual(hit["impact"], collect._IMPACT_NETPOL_PARTIAL)
        self.assertNotIn("Every pod in this namespace", hit["impact"])

        zero = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[],
            workloads=[{"kind": "Pod", "ns": "payments", "name": "api"}],
        )
        self.assertNotIn("impact", collect.check_netpol_missing(zero)[0])

        allow_all = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[netpol("open", ns="payments", pod_selector={}, ingress=[{}])],
            workloads=[{"kind": "Pod", "ns": "payments", "name": "api"}],
        )
        self.assertNotIn("impact", collect.check_netpol_missing(allow_all)[0])

    def test_the_excerpt_names_the_workload_and_the_object_stays_the_namespace(self):
        """A pod name carries a ReplicaSet hash and a random suffix. The ledger
        keys on the object, so a pod-scoped finding would resolve and re-raise
        on every rollout; the volatile name belongs in the excerpt, and even
        there it is the controller behind the pod that the reader has to go and
        edit."""
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[netpol("api", ns="payments", pod_selector={"matchLabels": {"app": "api"}}, policy_types=["Ingress"])],
            pods=[
                netpol_pod(
                    "web-6f8d9c4b5-xk2mn",
                    ns="payments",
                    labels={"app.kubernetes.io/name": "web"},
                    owners=[owner("ReplicaSet", "web-6f8d9c4b5")],
                )
            ],
            workloads=[{"kind": "Deployment", "ns": "payments", "name": "web"}],
            pod_namespaces={"payments"},
        )
        hits = collect.check_netpol_missing(ctx)
        self.assertEqual(hits[0]["object"], "Namespace/payments")
        self.assertIn("Deployment/web", hits[0]["excerpt"])
        self.assertNotIn("6f8d9c4b5", hits[0]["excerpt"])

    def test_a_namespace_whose_every_pod_is_selected_is_left_alone(self):
        """argocd on the reference fleet: seven pods, seven policies, each
        naming its own workload. The check has to stay silent there or it
        becomes the false-positive flood instead of the missing finding."""
        ctx = context_of(
            namespaces=[namespace("argocd")],
            networkpolicies=[
                netpol("server", ns="argocd", pod_selector={"matchLabels": {"app.kubernetes.io/name": "argocd-server"}}, policy_types=["Ingress"]),
                netpol("redis", ns="argocd", pod_selector={"matchLabels": {"app.kubernetes.io/name": "argocd-redis"}}, policy_types=["Ingress"]),
            ],
            pods=[
                netpol_pod("argocd-server-687f-z89zg", ns="argocd", labels={"app.kubernetes.io/name": "argocd-server"}),
                netpol_pod("argocd-redis-79db-g8xhl", ns="argocd", labels={"app.kubernetes.io/name": "argocd-redis"}),
            ],
            pod_namespaces={"argocd"},
        )
        self.assertEqual(collect.check_netpol_missing(ctx), [])

    def test_an_egress_only_policy_is_not_ingress_coverage(self):
        """§2.6 asks who can reach these pods. A policy whose `policyTypes` is
        Egress alone leaves its own pods exactly as reachable as they were."""
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[netpol("egress", ns="payments", pod_selector={"matchLabels": {"app": "api"}}, policy_types=["Egress"])],
            pods=[netpol_pod("api-1", ns="payments", labels={"app": "api"})],
            pod_namespaces={"payments"},
        )
        self.assertEqual(len(collect.check_netpol_missing(ctx)), 1)

    def test_an_absent_policy_types_still_counts_as_ingress_coverage(self):
        """Kubernetes derives `policyTypes` from the rule blocks present, and
        a spec with neither derives to `["Ingress"]` -- a deny-all, the
        strongest coverage there is. Absent must not read as egress-only."""
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[netpol("deny", ns="payments", pod_selector={"matchLabels": {"app": "api"}})],
            pods=[netpol_pod("api-1", ns="payments", labels={"app": "api"})],
            pod_namespaces={"payments"},
        )
        self.assertEqual(collect.check_netpol_missing(ctx), [])

    def test_a_finished_job_pod_is_neither_a_gap_nor_a_denominator(self):
        """`kubeagents-system` carries three Failed CronJob pods. A pod that is
        not running cannot be reached, so it is not exposure -- and its name is
        the churniest of all, one per schedule tick."""
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[netpol("api", ns="payments", pod_selector={"matchLabels": {"app": "api"}}, policy_types=["Ingress"])],
            pods=[
                netpol_pod("api-1", ns="payments", labels={"app": "api"}),
                netpol_pod("batch-29803800-892cg", ns="payments", labels={"job-name": "batch"}, phase="Failed"),
                netpol_pod("batch-29803860-ct6br", ns="payments", labels={"job-name": "batch"}, phase="Succeeded"),
            ],
            pod_namespaces={"payments"},
        )
        self.assertEqual(collect.check_netpol_missing(ctx), [])

    def test_an_allow_all_alongside_a_real_policy_is_still_the_allow_all_finding(self):
        """The two branches have to compose. `podSelector: {}` selects every
        pod in the namespace, so a namespace holding one is never a coverage
        gap -- and policies are additive, so the narrower policy beside it
        restricts nothing: it is the `minor` allow-all finding."""
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[
                netpol("allow-all", ns="payments", ingress=[{}]),
                netpol("api", ns="payments", pod_selector={"matchLabels": {"app": "api"}}, policy_types=["Ingress"]),
            ],
            pods=[netpol_pod("web-1", ns="payments", labels={"app": "web"})],
            pod_namespaces={"payments"},
        )
        hits = collect.check_netpol_missing(ctx)
        self.assertEqual([(h["object"], h["severity"]) for h in hits], [("NetworkPolicy/allow-all", "minor")])

    def test_a_cluster_network_policy_suppresses_a_partial_gap_too(self):
        """The Do-NOT-flag case does not stop applying because the namespace
        also has a namespaced policy of its own."""
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[netpol("api", ns="payments", pod_selector={"matchLabels": {"app": "api"}}, policy_types=["Ingress"])],
            pods=[netpol_pod("web-1", ns="payments", labels={"app": "web"})],
            pod_namespaces={"payments"},
            cluster_network_policies=[ccnp("fleet-wide")],
        )
        self.assertEqual(collect.check_netpol_missing(ctx), [])

    def test_an_unlabelled_pod_is_uncovered_and_named_by_its_pod_name(self):
        """Nothing but `podSelector: {}` can select a pod with no labels, so it
        is genuinely uncovered, and there is no workload label to name it by."""
        ctx = context_of(
            namespaces=[namespace("payments")],
            networkpolicies=[netpol("api", ns="payments", pod_selector={"matchLabels": {"app": "api"}}, policy_types=["Ingress"])],
            pods=[netpol_pod("bare", ns="payments", labels={})],
            pod_namespaces={"payments"},
        )
        hits = collect.check_netpol_missing(ctx)
        self.assertEqual(len(hits), 1)
        self.assertIn("bare", hits[0]["excerpt"])


class TestDefaultSaAutomount(unittest.TestCase):
    def test_default_sa_with_no_override_is_flagged(self):
        ctx = context_of(
            serviceaccounts=[default_sa("default")],
            workloads=[{"kind": "Pod", "ns": "default", "name": "api", "spec": {}}],
        )
        self.assertEqual(len(collect.check_default_sa_automount(ctx)), 1)

    def test_a_dedicated_service_account_is_never_flagged(self):
        ctx = context_of(
            serviceaccounts=[default_sa("default")],
            workloads=[{"kind": "Pod", "ns": "default", "name": "api", "spec": {"serviceAccountName": "api-sa"}}],
        )
        self.assertEqual(collect.check_default_sa_automount(ctx), [])

    def test_the_namespace_default_sa_disabling_automount_suppresses_it(self):
        ctx = context_of(
            serviceaccounts=[default_sa("default", automount=False)],
            workloads=[{"kind": "Pod", "ns": "default", "name": "api", "spec": {}}],
        )
        self.assertEqual(collect.check_default_sa_automount(ctx), [])

    def test_the_pod_level_override_suppresses_it_even_if_the_sa_does_not(self):
        ctx = context_of(
            serviceaccounts=[default_sa("default")],
            workloads=[
                {"kind": "Pod", "ns": "default", "name": "api", "spec": {"automountServiceAccountToken": False}}
            ],
        )
        self.assertEqual(collect.check_default_sa_automount(ctx), [])


class TestUnboundSaAutomount(unittest.TestCase):
    """§2.14 -- a named ServiceAccount no binding grants anything, mounted anyway.

    §2.7 stops at `default`, so every workload here is invisible to it: the
    chart created a ServiceAccount, which is the recommended thing to do, and
    then bound it to nothing and left the automount default alone.
    """

    @staticmethod
    def sa(name, ns="shop", automount=None):
        doc = {"kind": "ServiceAccount", "metadata": {"namespace": ns, "name": name}}
        if automount is not None:
            doc["automountServiceAccountToken"] = automount
        return doc

    @staticmethod
    def wl(sa_name="api-sa", ns="shop", **spec):
        if sa_name is not None:
            spec["serviceAccountName"] = sa_name
        return {"kind": "Deployment", "ns": ns, "name": "api", "spec": spec}

    @staticmethod
    def binding(sa_name, ns="shop", kind="RoleBinding", role="reader"):
        return {
            "kind": kind,
            "metadata": {"name": f"{sa_name}-{role}"},
            "roleRef": {"kind": "Role", "name": role},
            "subjects": [{"kind": "ServiceAccount", "namespace": ns, "name": sa_name}],
        }

    def ctx(self, **overrides):
        base = {
            "serviceaccounts": [self.sa("api-sa")],
            "workloads": [self.wl()],
            "rolebindings": [],
            "clusterrolebindings": [],
        }
        base.update(overrides)
        return context_of(**base)

    def test_a_named_sa_with_no_binding_at_all_is_flagged(self):
        (hit,) = collect.check_unbound_sa_automount(self.ctx())
        self.assertEqual(hit["object"], "Deployment/api")
        self.assertEqual(hit["namespace"], "shop")
        # The excerpt names the ServiceAccount, because the object name does
        # not: charts routinely name the two differently.
        self.assertIn("api-sa", hit["excerpt"])

    def test_a_rolebinding_naming_the_sa_suppresses_it(self):
        self.assertEqual(
            collect.check_unbound_sa_automount(self.ctx(rolebindings=[self.binding("api-sa")])), []
        )

    def test_a_rolebinding_subject_with_no_namespace_takes_the_bindings_own(self):
        # The API server reads an omitted subject namespace as the RoleBinding's.
        binding = self.binding("api-sa")
        binding["metadata"]["namespace"] = "shop"
        del binding["subjects"][0]["namespace"]
        self.assertEqual(collect.check_unbound_sa_automount(self.ctx(rolebindings=[binding])), [])

    def test_a_vault_injected_workload_is_not_flagged(self):
        # The injector's sidecar logs in to Vault with the mounted token, and
        # Vault reviews it with its own identity: no binding here, token used.
        wl = self.wl()
        wl["pod_annotations"] = {"vault.hashicorp.com/agent-inject": "true"}
        self.assertEqual(collect.check_unbound_sa_automount(self.ctx(workloads=[wl])), [])

    def test_a_vault_annotation_set_to_false_still_flags_it(self):
        wl = self.wl()
        wl["pod_annotations"] = {"vault.hashicorp.com/agent-inject": "false"}
        self.assertEqual(len(collect.check_unbound_sa_automount(self.ctx(workloads=[wl]))), 1)

    def test_normalization_carries_the_pod_template_annotations(self):
        item = {
            "kind": "Deployment", "metadata": {"namespace": "shop", "name": "api"},
            "spec": {"template": {"metadata": {"annotations": {"a": "b"}}, "spec": {}}},
        }
        cronjob = {
            "kind": "CronJob", "metadata": {"namespace": "shop", "name": "nightly"},
            "spec": {"jobTemplate": {"spec": {"template": {
                "metadata": {"annotations": {"c": "d"}}, "spec": {}}}}},
        }
        self.assertEqual(collect._pod_annotations_of(item), {"a": "b"})
        self.assertEqual(collect._pod_annotations_of(cronjob), {"c": "d"})
        pod = {"kind": "Pod", "metadata": {"annotations": {"e": "f"}}, "spec": {}}
        self.assertEqual(collect._pod_annotations_of(pod), {"e": "f"})
        # And the normalizer the check actually reads carries them through.
        (wl,) = collect.normalize_compliance_workloads(dump_of(item))
        self.assertEqual(wl["pod_annotations"], {"a": "b"})

    def test_only_the_injectors_own_spellings_of_true_skip_it(self):
        # `strconv.ParseBool` neither trims nor folds case beyond these three.
        for value, skipped in (("True", True), ("TRUE", True), ("1", True),
                               (" true", False), ("tRuE", False), ("yes", False)):
            with self.subTest(value=value):
                wl = self.wl()
                wl["pod_annotations"] = {"vault.hashicorp.com/agent-inject": value}
                hits = collect.check_unbound_sa_automount(self.ctx(workloads=[wl]))
                self.assertEqual(hits == [], skipped)

    def test_a_clusterrolebinding_naming_the_sa_suppresses_it(self):
        ctx = self.ctx(clusterrolebindings=[self.binding("api-sa", kind="ClusterRoleBinding")])
        self.assertEqual(collect.check_unbound_sa_automount(ctx), [])

    def test_a_binding_naming_the_same_sa_name_in_another_namespace_does_not(self):
        # ServiceAccount subjects are namespaced and the names collide across a
        # fleet -- `api-sa` in `staging` grants nothing to `api-sa` in `shop`.
        ctx = self.ctx(rolebindings=[self.binding("api-sa", ns="staging")])
        self.assertEqual(len(collect.check_unbound_sa_automount(ctx)), 1)

    def test_the_default_service_account_is_never_flagged(self):
        # §2.7's finding. Emitting both on one object would publish the same
        # remediation twice under two ids.
        for spec in ({}, {"serviceAccountName": "default"}):
            with self.subTest(spec=spec):
                ctx = self.ctx(
                    serviceaccounts=[default_sa("shop")],
                    workloads=[{"kind": "Deployment", "ns": "shop", "name": "api", "spec": spec}],
                )
                self.assertEqual(collect.check_unbound_sa_automount(ctx), [])

    def test_the_pod_level_override_suppresses_it(self):
        ctx = self.ctx(workloads=[self.wl(automountServiceAccountToken=False)])
        self.assertEqual(collect.check_unbound_sa_automount(ctx), [])

    def test_the_service_account_level_override_suppresses_it(self):
        ctx = self.ctx(serviceaccounts=[self.sa("api-sa", automount=False)])
        self.assertEqual(collect.check_unbound_sa_automount(ctx), [])

    def test_a_service_account_that_does_not_exist_is_not_flagged(self):
        # No object, no token, no pod -- the workload does not start. Sending a
        # pull request to disable a mount that is not happening would be a
        # finding about the wrong thing.
        self.assertEqual(collect.check_unbound_sa_automount(self.ctx(serviceaccounts=[])), [])

    def test_the_deprecated_service_account_field_is_read_too(self):
        ctx = self.ctx(workloads=[{"kind": "Deployment", "ns": "shop", "name": "api", "spec": {"serviceAccount": "api-sa"}}])
        self.assertEqual(len(collect.check_unbound_sa_automount(ctx)), 1)

    def test_the_finding_carries_the_workloads_reconciler(self):
        wl = self.wl()
        wl["reconciler"] = "argocd:workloads-shop"
        (hit,) = collect.check_unbound_sa_automount(self.ctx(workloads=[wl]))
        self.assertEqual(hit["reconciler"], "argocd:workloads-shop")

    def test_a_namespace_service_account_group_covers_only_that_namespace(self):
        # `system:serviceaccounts:shop` grants the SAs in `shop` and nobody
        # else; one team's grant must not silence the check fleet-wide.
        group = {
            "kind": "RoleBinding",
            "metadata": {"name": "shop-sas"},
            "roleRef": {"kind": "Role", "name": "reader"},
            "subjects": [{"kind": "Group", "name": "system:serviceaccounts:shop"}],
        }
        ctx = self.ctx(
            serviceaccounts=[self.sa("api-sa"), self.sa("api-sa", ns="billing")],
            workloads=[self.wl(), self.wl(ns="billing")],
            rolebindings=[group],
        )
        hits = collect.check_unbound_sa_automount(ctx)
        self.assertEqual([h["namespace"] for h in hits], ["billing"])

    def test_a_group_binding_to_every_service_account_suppresses_the_whole_cluster(self):
        # The correctness hole this check has to close. `view` bound to
        # `system:authenticated` grants every ServiceAccount read on the
        # cluster without naming one, so the unbound claim is false everywhere
        # and the check reports nothing rather than reporting something untrue.
        for group in ("system:authenticated", "system:serviceaccounts", "system:serviceaccounts:shop"):
            with self.subTest(group=group):
                ctx = self.ctx(
                    clusterrolebindings=[
                        {
                            "kind": "ClusterRoleBinding",
                            "metadata": {"name": "browsable"},
                            "roleRef": {"kind": "ClusterRole", "name": "view"},
                            "subjects": [{"kind": "Group", "name": group}],
                        }
                    ]
                )
                self.assertEqual(collect.check_unbound_sa_automount(ctx), [])

    def test_the_four_kubernetes_baseline_group_bindings_do_not(self):
        # Every cluster ships these, so counting them would retire the check
        # fleet-wide on day one. They grant discovery, `/version`, `/healthz`, a
        # self-review of one's own permissions, and the OIDC issuer document --
        # the baseline every authenticated principal already has.
        ctx = self.ctx(
            clusterrolebindings=[
                {
                    "kind": "ClusterRoleBinding",
                    "metadata": {"name": f"system:{role}"},
                    "roleRef": {"kind": "ClusterRole", "name": role},
                    "subjects": [{"kind": "Group", "name": "system:authenticated"}],
                }
                for role in (
                    "system:basic-user",
                    "system:discovery",
                    "system:public-info-viewer",
                    "system:service-account-issuer-discovery",
                )
            ]
        )
        self.assertEqual(len(collect.check_unbound_sa_automount(ctx)), 1)

    def test_a_group_binding_to_an_ordinary_group_is_not_a_suppression(self):
        # `platform-team@acme.com` is a set of humans, not the service accounts.
        ctx = self.ctx(
            clusterrolebindings=[
                {
                    "kind": "ClusterRoleBinding",
                    "metadata": {"name": "platform-admin"},
                    "roleRef": {"kind": "ClusterRole", "name": "edit"},
                    "subjects": [{"kind": "Group", "name": "platform-team@acme.com"}],
                }
            ]
        )
        self.assertEqual(len(collect.check_unbound_sa_automount(ctx)), 1)

    def test_it_is_on_the_compliance_roster(self):
        spec = next(c for c in collect.COMPLIANCE_CHECKS if c.slug == "unbound-sa-automount")
        self.assertEqual(spec.kind, "cluster")
        self.assertEqual(spec.severity, "major")


class TestWorkloadIdentityOff(unittest.TestCase):
    def test_empty_workload_pool_is_flagged(self):
        ctx = context_of(cluster_describe={"workloadIdentityConfig": {}})
        self.assertEqual(len(collect.check_workload_identity_off(ctx)), 1)

    def test_a_set_workload_pool_is_never_flagged(self):
        ctx = context_of(cluster_describe={"workloadIdentityConfig": {"workloadPool": "acme.svc.id.goog"}})
        self.assertEqual(collect.check_workload_identity_off(ctx), [])


class TestClusterScopedObject(unittest.TestCase):
    """The object of a cluster-scoped finding is `Cluster/<name>`, never `Cluster`.

    Both checks below emitted the bare kind until 2026-08-29. The finding id
    derives from `object`, so the day the collector started supplying it the
    compliance ledger announced four unchanged public control planes as
    resolved and re-opened them as new.
    """

    def test_both_cluster_scoped_checks_name_the_cluster(self):
        cases = (
            (
                collect.check_workload_identity_off,
                {"workloadIdentityConfig": {}},
            ),
            (
                collect.check_public_control_plane,
                {"privateClusterConfig": {}, "masterAuthorizedNetworksConfig": {}},
            ),
        )
        for check, describe in cases:
            with self.subTest(check=check.__name__):
                ctx = context_of(cluster_describe=describe)
                ctx["cluster_name"] = "kube-agents-host"
                (hit,) = check(ctx)
                self.assertEqual(hit["object"], "Cluster/kube-agents-host")

    def test_a_context_with_no_cluster_name_fails_the_cluster_closed(self):
        # Rather than emitting a nameless object that `audit_report` would
        # refuse at publish time, fifty minutes later.
        ctx = context_of(cluster_describe={"workloadIdentityConfig": {}})
        ctx["cluster_name"] = ""
        with self.assertRaises(collect.GateFailure):
            collect.check_workload_identity_off(ctx)

    def test_the_real_context_builder_supplies_it(self):
        # `context_of` is a test double; the assertion above is only worth
        # anything if the production builder sets the same key.
        source = inspect.getsource(collect._collect_compliance)
        self.assertIn('"cluster_name": name', source)


class TestLegacyMetadata(unittest.TestCase):
    def test_gce_metadata_mode_is_flagged(self):
        ctx = context_of(node_pools=[{"name": "pool-1", "config": {"workloadMetadataConfig": {"mode": "GCE_METADATA"}}}])
        self.assertEqual(len(collect.check_legacy_metadata(ctx)), 1)

    def test_empty_mode_is_flagged(self):
        ctx = context_of(node_pools=[{"name": "pool-1", "config": {}}])
        self.assertEqual(len(collect.check_legacy_metadata(ctx)), 1)

    def test_gke_metadata_mode_is_never_flagged(self):
        ctx = context_of(node_pools=[{"name": "pool-1", "config": {"workloadMetadataConfig": {"mode": "GKE_METADATA"}}}])
        self.assertEqual(collect.check_legacy_metadata(ctx), [])


class TestPublicControlPlane(unittest.TestCase):
    def test_public_endpoint_with_no_restriction_is_flagged(self):
        ctx = context_of(cluster_describe={"privateClusterConfig": {}, "masterAuthorizedNetworksConfig": {}})
        self.assertEqual(len(collect.check_public_control_plane(ctx)), 1)

    def test_public_endpoint_with_unrestricted_cidr_is_flagged(self):
        """`cidrBlocks` carries CidrBlock objects, so a string fixture proves nothing.

        The GKE discovery document types this array as `CidrBlock`
        (`{displayName, cidrBlock}`) and the API never emits bare strings. A
        membership test for the string therefore could not match, and a cluster
        that turned authorized networks on and then allowed the whole internet
        -- the single configuration this branch exists to catch -- was reported
        as restricted.
        """
        ctx = context_of(
            cluster_describe={
                "privateClusterConfig": {},
                "masterAuthorizedNetworksConfig": {
                    "enabled": True,
                    "cidrBlocks": [{"displayName": "everywhere", "cidrBlock": "0.0.0.0/0"}],
                },
            }
        )
        self.assertEqual(len(collect.check_public_control_plane(ctx)), 1)

    def test_the_v6_default_route_is_allow_all_too(self):
        # A dual-stack cluster can write `::/0` where only `0.0.0.0/0` was ever
        # recognised. Matching the v4 string alone reads that as an allowlist,
        # drops the finding, and loses a `critical` on a control plane open to
        # every IPv6 address on the internet.
        for block in ("::/0", "0.0.0.0/0"):
            with self.subTest(block=block):
                ctx = context_of(
                    cluster_describe={
                        "privateClusterConfig": {},
                        "masterAuthorizedNetworksConfig": {
                            "enabled": True,
                            "cidrBlocks": [
                                {"displayName": "office", "cidrBlock": "203.0.113.0/24"},
                                {"displayName": "everywhere", "cidrBlock": block},
                            ],
                        },
                    }
                )
                self.assertEqual(len(collect.check_public_control_plane(ctx)), 1)

    def test_enabled_with_no_cidr_blocks_stays_restrictive(self):
        # Authorized networks on with an empty list allowlists nothing, so the
        # endpoint is shut. Treating "no blocks" as "nothing was allowlisted,
        # so it is open" would invert it and report the most locked-down
        # clusters. Google Cloud access has to be off for that to hold: with
        # it on, the empty list is the only thing between the endpoint and
        # every VM on Google Cloud, and it is not in the way.
        ctx = context_of(
            cluster_describe={
                "privateClusterConfig": {},
                "masterAuthorizedNetworksConfig": {
                    "enabled": True,
                    "cidrBlocks": [],
                    "gcpPublicCidrsAccessEnabled": False,
                },
            }
        )
        self.assertEqual(collect.check_public_control_plane(ctx), [])

    def test_a_config_present_but_not_enabled_does_not_count_as_restrictive(self):
        # What `kube-agents-host` actually returns: a non-empty
        # `masterAuthorizedNetworksConfig` carrying only
        # `gcpPublicCidrsAccessEnabled`, with `enabled` absent. Testing the
        # object for emptiness rather than for `enabled` would call that
        # cluster restricted and lose the finding.
        ctx = context_of(
            cluster_describe={
                "privateClusterConfig": {},
                "masterAuthorizedNetworksConfig": {"gcpPublicCidrsAccessEnabled": True},
                "controlPlaneEndpointsConfig": {
                    "ipEndpointsConfig": {
                        "enablePublicEndpoint": True,
                        "authorizedNetworksConfig": {"gcpPublicCidrsAccessEnabled": True},
                    }
                },
            }
        )
        self.assertEqual(len(collect.check_public_control_plane(ctx)), 1)

    def test_the_excerpt_names_the_field_that_decided_the_verdict(self):
        """Two clusters that both fire must not produce the same evidence.

        This excerpt used to be the constant sentence "public endpoint
        reachable with no restrictive authorized networks", and
        `adopt_collector_evidence` overwrites whatever the model measured with
        it -- so on a live fleet of sixteen clusters all sixteen findings
        carried byte-identical evidence naming no field, no value, and no
        cluster. A reader could not check one against the API, and the two
        shapes below, which are materially different postures reached through
        different fields, were indistinguishable in the ledger.
        """
        current = collect.check_public_control_plane(
            context_of(
                cluster_describe={
                    "masterAuthorizedNetworksConfig": {"gcpPublicCidrsAccessEnabled": True},
                    "controlPlaneEndpointsConfig": {"ipEndpointsConfig": {"enablePublicEndpoint": True}},
                }
            )
        )
        legacy = collect.check_public_control_plane(
            context_of(cluster_describe={"privateClusterConfig": {}, "masterAuthorizedNetworksConfig": {}})
        )
        self.assertEqual((len(current), len(legacy)), (1, 1))
        self.assertNotEqual(current[0]["excerpt"], legacy[0]["excerpt"])

        # The deciding field, spelled the way the JSON read spelled it.
        self.assertIn(
            "controlPlaneEndpointsConfig.ipEndpointsConfig.enablePublicEndpoint=true",
            current[0]["excerpt"],
        )
        self.assertIn("privateClusterConfig.enablePrivateEndpoint=absent", legacy[0]["excerpt"])

        # Absent is not `false`: a field GKE omitted has to read as omitted, or
        # the excerpt asserts a value nobody observed.
        self.assertIn("masterAuthorizedNetworksConfig.enabled=absent", current[0]["excerpt"])
        self.assertIn("gcpPublicCidrsAccessEnabled=true", current[0]["excerpt"])
        self.assertNotIn("gcpPublicCidrsAccessEnabled", legacy[0]["excerpt"])

        # Both surfaces are named even where GKE returned only one of them, so
        # "not mentioned" cannot be confused with "not read".
        for excerpt in (current[0]["excerpt"], legacy[0]["excerpt"]):
            self.assertIn("ipEndpointsConfig.authorizedNetworksConfig.enabled=", excerpt)

    def test_an_external_dns_endpoint_survives_restrictive_authorized_networks(self):
        """The one shape where silence here was a false negative.

        Authorized networks gates the IP endpoint and nothing else. A cluster
        that allowlists its IP endpoint and serves the DNS endpoint to external
        traffic is still answering the internet, and the operator who enabled
        authorized networks to close it has not.
        """
        found = collect.check_public_control_plane(
            context_of(
                cluster_describe={
                    "privateClusterConfig": {},
                    "masterAuthorizedNetworksConfig": {
                        "enabled": True,
                        "cidrBlocks": [{"displayName": "office", "cidrBlock": "203.0.113.0/24"}],
                        "gcpPublicCidrsAccessEnabled": False,
                    },
                    "controlPlaneEndpointsConfig": {
                        "ipEndpointsConfig": {"enablePublicEndpoint": True},
                        "dnsEndpointConfig": {"allowExternalTraffic": True},
                    },
                }
            )
        )
        self.assertEqual(len(found), 1)
        self.assertIn("dnsEndpointConfig.allowExternalTraffic=true", found[0]["excerpt"])
        # The allowlisted IP path is closed, so claiming it is open would send
        # the reader to fix a setting that is already right.
        self.assertNotIn("enablePublicEndpoint", found[0]["excerpt"])

    def test_a_dns_only_cluster_is_not_called_reachable_over_an_ip_it_lacks(self):
        """`ipEndpointsConfig.enabled` is the switch `enablePublicEndpoint` sits under.

        `--no-enable-ip-access` serves no IP endpoint at all, and GKE leaves
        the now-moot `enablePublicEndpoint` behind it.
        """
        self.assertEqual(
            collect.check_public_control_plane(
                context_of(
                    cluster_describe={
                        "privateClusterConfig": {},
                        "masterAuthorizedNetworksConfig": {},
                        "controlPlaneEndpointsConfig": {
                            "ipEndpointsConfig": {"enabled": False, "enablePublicEndpoint": True},
                            "dnsEndpointConfig": {"allowExternalTraffic": False},
                        },
                    }
                )
            ),
            [],
        )

    def test_a_dns_only_cluster_open_externally_is_flagged_for_that_path_alone(self):
        found = collect.check_public_control_plane(
            context_of(
                cluster_describe={
                    "privateClusterConfig": {},
                    "masterAuthorizedNetworksConfig": {},
                    "controlPlaneEndpointsConfig": {
                        "ipEndpointsConfig": {"enabled": False, "enablePublicEndpoint": True},
                        "dnsEndpointConfig": {"allowExternalTraffic": True},
                    },
                }
            )
        )
        self.assertEqual(len(found), 1)
        self.assertIn("dnsEndpointConfig.allowExternalTraffic=true", found[0]["excerpt"])
        self.assertNotIn("enablePublicEndpoint", found[0]["excerpt"])
        # No IP endpoint means no allowlist to cite: the lead says why the IP
        # path is absent rather than claiming a control nobody configured.
        self.assertTrue(found[0]["impact"].startswith("The cluster serves no public IP endpoint"))
        self.assertNotIn("allowlisted", found[0]["impact"])

    def test_both_paths_open_names_both(self):
        found = collect.check_public_control_plane(
            context_of(
                cluster_describe={
                    "privateClusterConfig": {},
                    "masterAuthorizedNetworksConfig": {},
                    "controlPlaneEndpointsConfig": {
                        "ipEndpointsConfig": {"enablePublicEndpoint": True},
                        "dnsEndpointConfig": {"allowExternalTraffic": True},
                    },
                }
            )
        )
        self.assertEqual(len(found), 1)
        self.assertIn("enablePublicEndpoint=true", found[0]["excerpt"])
        self.assertIn("dnsEndpointConfig.allowExternalTraffic=true", found[0]["excerpt"])

    def test_the_dns_only_arm_does_not_claim_an_unauthenticated_api_server(self):
        """The IP endpoint is allowlisted, so the finding must not describe it as open.

        Reaching the DNS endpoint costs an attacker a Google identity first;
        saying "any address on the internet" of this cluster overstates the one
        path that is left and points the reader at a control already correct.
        """
        found = collect.check_public_control_plane(
            context_of(
                cluster_describe={
                    "privateClusterConfig": {},
                    "masterAuthorizedNetworksConfig": {
                        "enabled": True,
                        "cidrBlocks": [{"cidrBlock": "203.0.113.0/24"}],
                        "gcpPublicCidrsAccessEnabled": False,
                    },
                    "controlPlaneEndpointsConfig": {
                        "ipEndpointsConfig": {"enablePublicEndpoint": True},
                        "dnsEndpointConfig": {"allowExternalTraffic": True},
                    },
                }
            )
        )
        self.assertEqual(len(found), 1)
        impact = found[0]["impact"]
        self.assertIn("container.clusters.connect", impact)
        self.assertIn("Authorized networks do not gate it", impact)
        # The IP-endpoint sentence would send the reader to widen or narrow a
        # list that changes nothing about this path.
        self.assertNotIn("directly exploitable", impact)
        self.assertTrue(impact.startswith("The IP endpoint is allowlisted, but the cluster also serves a DNS"))

    def test_an_open_ip_endpoint_keeps_the_unauthenticated_arm_even_beside_dns(self):
        """DNS being open too does not soften the IP endpoint's own exposure."""
        for label, endpoints in (
            ("ip alone", {"ipEndpointsConfig": {"enablePublicEndpoint": True}}),
            (
                "ip and dns",
                {
                    "ipEndpointsConfig": {"enablePublicEndpoint": True},
                    "dnsEndpointConfig": {"allowExternalTraffic": True},
                },
            ),
        ):
            with self.subTest(label):
                found = collect.check_public_control_plane(
                    context_of(
                        cluster_describe={
                            "privateClusterConfig": {},
                            "masterAuthorizedNetworksConfig": {},
                            "controlPlaneEndpointsConfig": endpoints,
                        }
                    )
                )
                self.assertEqual(len(found), 1)
                self.assertIn("directly exploitable", found[0]["impact"])

    def test_google_cloud_access_is_marked_inert_when_there_is_no_allowlist(self):
        """It grants an exception to an allowlist that is not switched on.

        Unannotated it was the only difference between one cluster's excerpt
        and fifteen identical ones, reading as an aggravating factor on a
        cluster no worse than its neighbours.
        """
        found = collect.check_public_control_plane(
            context_of(
                cluster_describe={
                    "privateClusterConfig": {},
                    "masterAuthorizedNetworksConfig": {"gcpPublicCidrsAccessEnabled": True},
                }
            )
        )
        self.assertIn("gcpPublicCidrsAccessEnabled=true (inert:", found[0]["excerpt"])

    def test_google_cloud_access_is_not_marked_inert_beside_a_live_allowlist(self):
        found = collect.check_public_control_plane(
            context_of(
                cluster_describe={
                    "privateClusterConfig": {},
                    "masterAuthorizedNetworksConfig": {
                        "enabled": True,
                        "gcpPublicCidrsAccessEnabled": True,
                        "cidrBlocks": [{"displayName": "everywhere", "cidrBlock": "0.0.0.0/0"}],
                    },
                }
            )
        )
        self.assertIn("gcpPublicCidrsAccessEnabled=true", found[0]["excerpt"])
        self.assertNotIn("inert", found[0]["excerpt"])

    def test_the_excerpt_quotes_the_cidr_that_made_it_unrestricted(self):
        # A cluster caught by the allow-all branch is caught *because of* a
        # specific block. Leaving it out of the excerpt makes the one finding
        # whose evidence is genuinely checkable read like the ones that are not.
        found = collect.check_public_control_plane(
            context_of(
                cluster_describe={
                    "privateClusterConfig": {},
                    "masterAuthorizedNetworksConfig": {
                        "enabled": True,
                        "cidrBlocks": [
                            {"displayName": "office", "cidrBlock": "203.0.113.0/24"},
                            {"displayName": "everywhere", "cidrBlock": "::/0"},
                        ],
                    },
                }
            )
        )
        self.assertEqual(len(found), 1)
        self.assertIn("masterAuthorizedNetworksConfig.enabled=true", found[0]["excerpt"])
        self.assertIn("cidrBlocks=[203.0.113.0/24,::/0]", found[0]["excerpt"])

    def test_a_private_endpoint_is_never_flagged(self):
        ctx = context_of(cluster_describe={"privateClusterConfig": {"enablePrivateEndpoint": True}})
        self.assertEqual(collect.check_public_control_plane(ctx), [])

    def test_the_public_endpoint_turned_off_the_current_way_is_never_flagged(self):
        """`enablePublicEndpoint: false` is the whole answer where GKE returns it.

        The legacy `privateClusterConfig` block keeps coming back on a cluster
        that has none of it set -- GKE fills in the addresses and nothing else
        -- so a cluster that closed its public endpoint on the current surface
        carries no `enablePrivateEndpoint: true` to find. Reading the two as an
        `or` therefore reported it reachable from the internet at `critical`,
        which is the reverse of what it had configured.
        """
        for label, private_cfg in (
            ("legacy block absent", {}),
            ("legacy block present but only addressed", {"privateEndpoint": "10.0.0.2", "publicEndpoint": ""}),
        ):
            with self.subTest(legacy=label):
                ctx = context_of(
                    cluster_describe={
                        "privateClusterConfig": private_cfg,
                        "controlPlaneEndpointsConfig": {"ipEndpointsConfig": {"enablePublicEndpoint": False}},
                    }
                )
                self.assertEqual(collect.check_public_control_plane(ctx), [])

    def test_the_current_field_outranks_a_stale_legacy_one(self):
        # The other direction, so the fix is a precedence rule rather than a
        # second way to reach "not flagged": a cluster still serving the public
        # endpoint is flagged whatever the legacy block claims.
        ctx = context_of(
            cluster_describe={
                "privateClusterConfig": {"enablePrivateEndpoint": True},
                "controlPlaneEndpointsConfig": {"ipEndpointsConfig": {"enablePublicEndpoint": True}},
            }
        )
        self.assertEqual(len(collect.check_public_control_plane(ctx)), 1)

    def test_a_narrow_authorized_cidr_is_never_flagged(self):
        ctx = context_of(
            cluster_describe={
                "privateClusterConfig": {},
                "masterAuthorizedNetworksConfig": {
                    "enabled": True,
                    "cidrBlocks": [{"displayName": "corp", "cidrBlock": "10.0.0.0/8"}],
                    "gcpPublicCidrsAccessEnabled": False,
                },
            }
        )
        self.assertEqual(collect.check_public_control_plane(ctx), [])

    def test_google_cloud_access_defeats_the_narrowest_allowlist(self):
        """A `/32` allowlist is not a restriction while the grant is on.

        `gcpPublicCidrsAccessEnabled` excepts every external address Google
        Cloud owns -- the VMs, Cloud Run services and Cloud Functions of every
        project, not this one's -- so an attacker starts a VM and is inside the
        list. Suppressing on it graded that cluster clean.
        """
        for label, value in (("explicit", True), ("absent, and the API default is on", None)):
            cfg = {"enabled": True, "cidrBlocks": [{"cidrBlock": "203.0.113.7/32"}]}
            if value is not None:
                cfg["gcpPublicCidrsAccessEnabled"] = value
            with self.subTest(label):
                found = collect.check_public_control_plane(
                    context_of(
                        cluster_describe={
                            "privateClusterConfig": {},
                            "masterAuthorizedNetworksConfig": cfg,
                            "controlPlaneEndpointsConfig": {
                                "ipEndpointsConfig": {"enablePublicEndpoint": True}
                            },
                        }
                    )
                )
                self.assertEqual(len(found), 1)
                self.assertIn("enablePublicEndpoint=true", found[0]["excerpt"])

    def test_the_grant_is_named_in_the_excerpt_even_where_gke_returned_no_field(self):
        # `adopt_collector_evidence` overwrites the model's excerpt with this
        # one, so a reader who sees a tight `cidrBlocks` and no mention of the
        # grant concludes the opposite of the truth and has no second source.
        found = collect.check_public_control_plane(
            context_of(
                cluster_describe={
                    "privateClusterConfig": {},
                    "masterAuthorizedNetworksConfig": {
                        "enabled": True,
                        "cidrBlocks": [{"cidrBlock": "203.0.113.7/32"}],
                    },
                }
            )
        )
        self.assertIn("gcpPublicCidrsAccessEnabled=absent", found[0]["excerpt"])
        self.assertIn("admits every Google Cloud external IP", found[0]["excerpt"])

    def test_the_grant_arm_does_not_claim_the_whole_internet(self):
        """The operator here already turned authorized networks on.

        Telling them the endpoint answers every address reads as a check that
        did not notice, and points at the control they have already set rather
        than at the flag still to set.
        """
        found = collect.check_public_control_plane(
            context_of(
                cluster_describe={
                    "privateClusterConfig": {},
                    "masterAuthorizedNetworksConfig": {
                        "enabled": True,
                        "cidrBlocks": [{"cidrBlock": "203.0.113.7/32"}],
                        "gcpPublicCidrsAccessEnabled": True,
                    },
                }
            )
        )
        impact = found[0]["impact"]
        self.assertIn("gcpPublicCidrsAccessEnabled", impact)
        self.assertNotIn("any address on the internet", impact)

    def test_an_allow_all_block_keeps_the_whole_internet_arm(self):
        # `0.0.0.0/0` beside the grant is not the grant's doing, and the
        # narrower sentence would understate it.
        found = collect.check_public_control_plane(
            context_of(
                cluster_describe={
                    "privateClusterConfig": {},
                    "masterAuthorizedNetworksConfig": {
                        "enabled": True,
                        "cidrBlocks": [{"cidrBlock": "0.0.0.0/0"}],
                        "gcpPublicCidrsAccessEnabled": True,
                    },
                }
            )
        )
        self.assertIn("directly exploitable", found[0]["impact"])

    def test_turning_the_grant_off_is_what_clears_the_finding(self):
        """The two states differ by one field, so the fix is verifiable.

        A remediation the check cannot see the effect of is one an operator
        applies forever. `--enable-master-authorized-networks` alone leaves the
        cluster flagged; adding `--no-enable-google-cloud-access` clears it.
        """
        def described(grant):
            return context_of(
                cluster_describe={
                    "privateClusterConfig": {},
                    "masterAuthorizedNetworksConfig": {
                        "enabled": True,
                        "cidrBlocks": [{"cidrBlock": "203.0.113.7/32"}],
                        "gcpPublicCidrsAccessEnabled": grant,
                    },
                }
            )

        self.assertEqual(len(collect.check_public_control_plane(described(True))), 1)
        self.assertEqual(collect.check_public_control_plane(described(False)), [])

    def test_authorized_networks_on_the_ip_endpoints_surface_are_honoured(self):
        """Setting both surfaces is invalid, so the newer one has to be read too.

        `IPEndpointsConfig.authorizedNetworksConfig` is where a cluster on the
        current API surface keeps this, and the discovery document says
        specifying it alongside `Cluster.masterAuthorizedNetworksConfig` is
        invalid. Reading only the legacy field reported every such cluster as
        having no restriction at all -- a critical raised against a control
        plane that is in fact closed.
        """
        ctx = context_of(
            cluster_describe={
                "privateClusterConfig": {},
                "controlPlaneEndpointsConfig": {
                    "ipEndpointsConfig": {
                        "enablePublicEndpoint": True,
                        "authorizedNetworksConfig": {
                            "enabled": True,
                            "cidrBlocks": [{"displayName": "corp", "cidrBlock": "10.0.0.0/8"}],
                            "gcpPublicCidrsAccessEnabled": False,
                        },
                    }
                },
            }
        )
        self.assertEqual(collect.check_public_control_plane(ctx), [])

    def test_an_unrestricted_cidr_on_the_ip_endpoints_surface_is_flagged(self):
        ctx = context_of(
            cluster_describe={
                "privateClusterConfig": {},
                "controlPlaneEndpointsConfig": {
                    "ipEndpointsConfig": {
                        "enablePublicEndpoint": True,
                        "authorizedNetworksConfig": {
                            "enabled": True,
                            "cidrBlocks": [{"displayName": "everywhere", "cidrBlock": "0.0.0.0/0"}],
                        },
                    }
                },
            }
        )
        self.assertEqual(len(collect.check_public_control_plane(ctx)), 1)


class TestPodSecurityGaps(unittest.TestCase):
    # Every container-level setting the restricted Pod Security Standard
    # requires. A fixture short of one of these is a non-compliant container,
    # so "compliant" has to name them all or the control tests are asserting
    # against the check's blind spot rather than against compliance.
    COMPLIANT = {
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "seccompProfile": {"type": "RuntimeDefault"},
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
    }

    def wl(self, container_sc=None, pod_sc=None):
        d = compliance_pod("x")
        if container_sc is not None:
            d["spec"]["containers"][0]["securityContext"] = container_sc
        if pod_sc is not None:
            d["spec"]["securityContext"] = pod_sc
        return collect.normalize_compliance_workloads(dump_of(d))[0]

    def test_no_security_context_at_all_is_flagged(self):
        self.assertIsNotNone(collect.check_podsecurity_gaps(self.wl(), context_of()))

    def test_full_compliant_context_is_not_flagged(self):
        self.assertIsNone(collect.check_podsecurity_gaps(self.wl(container_sc=self.COMPLIANT), context_of()))

    def test_explicit_false_over_a_compliant_pod_default_is_still_flagged(self):
        # The has()-vs-// distinction the SOP is emphatic about: a container
        # explicitly setting runAsNonRoot: false must not inherit a
        # compliant pod-level true.
        wl = self.wl(container_sc={**self.COMPLIANT, "runAsNonRoot": False}, pod_sc={"runAsNonRoot": True})
        hit = collect.check_podsecurity_gaps(wl, context_of())
        self.assertEqual(hit["excerpt"], "containers: app (runAsNonRoot=false)")

    def test_runAsUser_zero_is_flagged_even_with_nonroot_true(self):
        hit = collect.check_podsecurity_gaps(self.wl(container_sc={**self.COMPLIANT, "runAsUser": 0}), context_of())
        self.assertEqual(hit["excerpt"], "containers: app (runAsUser=0)")

    def test_the_excerpt_names_which_of_the_five_settings_failed(self):
        """Five independent settings decide this check, and the excerpt is
        published verbatim -- `adopt_collector_evidence` overwrites whatever the
        model wrote with it. A bare container name would tell a reader a
        workload is non-compliant without telling them what to change, and the
        fix for `runAsUser=0` is not the fix for a missing seccomp profile."""
        only_uid = collect.check_podsecurity_gaps(
            self.wl(container_sc={**self.COMPLIANT, "runAsUser": 0}), context_of()
        )
        self.assertEqual(only_uid["excerpt"], "containers: app (runAsUser=0)")

        only_caps = collect.check_podsecurity_gaps(
            self.wl(container_sc={**self.COMPLIANT, "capabilities": {"drop": ["NET_RAW"]}}), context_of()
        )
        self.assertEqual(only_caps["excerpt"], 'containers: app (capabilities.drop=["NET_RAW"])')

        everything = collect.check_podsecurity_gaps(self.wl(), context_of())
        self.assertEqual(
            everything["excerpt"],
            "containers: app (runAsNonRoot=null, seccompProfile.type=absent, "
            "allowPrivilegeEscalation=null, capabilities.drop=[])",
        )

    def test_missing_seccomp_profile_is_flagged(self):
        sc = {k: v for k, v in self.COMPLIANT.items() if k != "seccompProfile"}
        self.assertIsNotNone(collect.check_podsecurity_gaps(self.wl(container_sc=sc), context_of()))

    def test_privilege_escalation_left_enabled_is_flagged(self):
        """A container hardened on the other four still escalates to root the
        moment a setuid binary runs, which is the whole point of the setting."""
        sc = {k: v for k, v in self.COMPLIANT.items() if k != "allowPrivilegeEscalation"}
        hit = collect.check_podsecurity_gaps(self.wl(container_sc=sc), context_of())
        self.assertEqual(hit["excerpt"], "containers: app (allowPrivilegeEscalation=null)")

    def test_retained_capabilities_are_flagged(self):
        """`drop: [ALL]` is what restricted requires; dropping some of them is
        not most of the way there, it is a container that kept CAP_NET_ADMIN."""
        sc = {**self.COMPLIANT, "capabilities": {"drop": ["NET_RAW", "SYS_CHROOT"]}}
        self.assertIsNotNone(collect.check_podsecurity_gaps(self.wl(container_sc=sc), context_of()))

    def test_dropping_all_in_lower_case_still_counts(self):
        sc = {**self.COMPLIANT, "capabilities": {"drop": ["all"]}}
        self.assertIsNone(collect.check_podsecurity_gaps(self.wl(container_sc=sc), context_of()))

    def test_pod_level_inheritance_is_honored_when_container_is_silent(self):
        """Only for the fields that have it. `runAsNonRoot`, `runAsUser` and
        `seccompProfile` exist on `PodSecurityContext` and inherit; the
        container is silent on all three here and still grades clean."""
        inheritable = {"runAsNonRoot": True, "runAsUser": 10001, "seccompProfile": {"type": "RuntimeDefault"}}
        container_only = {k: v for k, v in self.COMPLIANT.items() if k not in inheritable}
        self.assertIsNone(
            collect.check_podsecurity_gaps(self.wl(container_sc=container_only, pod_sc=inheritable), context_of())
        )

    def test_allow_privilege_escalation_does_not_inherit_from_the_pod(self):
        """`PodSecurityContext` carries neither `allowPrivilegeEscalation` nor
        `capabilities`, so a pod-level value is not a value the kubelet reads.
        Falling back to one would grade a container clean on a setting nothing
        applied to it."""
        container_only = {k: v for k, v in self.COMPLIANT.items() if k != "allowPrivilegeEscalation"}
        hit = collect.check_podsecurity_gaps(
            self.wl(container_sc=container_only, pod_sc={"allowPrivilegeEscalation": False}), context_of()
        )
        self.assertIsNotNone(hit)
        self.assertIn("allowPrivilegeEscalation", hit["excerpt"])

    def test_already_flagged_by_privileged_container_is_suppressed_here(self):
        d = compliance_pod("x")
        d["spec"]["containers"][0]["securityContext"] = {"privileged": True}
        wl = collect.normalize_compliance_workloads(dump_of(d))[0]
        self.assertIsNone(collect.check_podsecurity_gaps(wl, context_of()))

    def test_a_restricted_labelled_namespace_is_suppressed(self):
        ctx = context_of(namespaces=[namespace("default", labels={"pod-security.kubernetes.io/enforce": "restricted"})])
        self.assertIsNone(collect.check_podsecurity_gaps(self.wl(), ctx))


DIGEST_A = "gcr.io/acme/app@sha256:" + "a" * 64
DIGEST_B = "gcr.io/acme/app@sha256:" + "b" * 64


class TestImageFloatingTag(unittest.TestCase):
    """§2.13. The image reference that does not name specific bytes, and the
    digest that makes pinning it a no-op diff."""

    def workload(self, image, kind="Deployment", name="api", init=None):
        doc = compliance_workload(kind, name)
        pod_spec_of(doc)["containers"][0]["image"] = image
        if init is not None:
            pod_spec_of(doc)["initContainers"] = [{"name": "setup", "image": init}]
        return collect.normalize_compliance_workloads(dump_of(doc))[0]

    def running(self, *digests, container="app", owner="Deployment", name="api", phase="Running"):
        """One live pod per digest, owned by the workload under test."""
        return [
            {
                "ns": "default",
                "name": f"api-abc-{i}",
                "labels": {},
                "phase": phase,
                "owners": [{"kind": owner, "name": name}],
                "images": {container: d},
            }
            for i, d in enumerate(digests)
        ]

    def hit(self, image, pods=(), **kw):
        return collect.check_image_floating_tag(
            self.workload(image, **kw), {"pods": list(pods), "workloads": []}
        )

    def test_latest_is_flagged(self):
        hit = self.hit("gcr.io/acme/app:latest")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["object"], "Deployment/api")

    def test_the_other_moving_tags_are_flagged(self):
        for tag in ("main", "master", "dev", "nightly", "stable", "edge"):
            with self.subTest(tag=tag):
                self.assertIsNotNone(self.hit(f"gcr.io/acme/app:{tag}"))

    def test_no_tag_at_all_is_flagged(self):
        # The runtime resolves it to `:latest`, so it is the same defect
        # written more briefly.
        self.assertIsNotNone(self.hit("gcr.io/acme/app"))

    def test_a_registry_port_is_not_read_as_a_tag(self):
        # `registry:5000/app` has a colon and no tag; the tag is only what
        # follows the last slash.
        self.assertIsNotNone(self.hit("registry:5000/acme/app"))

    def test_a_digest_reference_is_never_flagged(self):
        self.assertIsNone(self.hit(DIGEST_A))

    def test_a_tag_pinned_by_digest_is_never_flagged(self):
        self.assertIsNone(self.hit("gcr.io/acme/app:1.4.2@sha256:" + "c" * 64))

    def test_a_version_shaped_tag_is_never_flagged(self):
        # Out of scope on purpose: `v2` and `python3.12` are mutable in
        # practice and indistinguishable from a pin by shape, so flagging them
        # would fire on every container in every cluster.
        for tag in ("1.4.2", "v2", "stable-3", "python3.12"):
            with self.subTest(tag=tag):
                self.assertIsNone(self.hit(f"gcr.io/acme/app:{tag}"))

    def test_an_init_container_is_flagged_too(self):
        hit = self.hit(DIGEST_A, init="busybox:latest")
        self.assertIsNotNone(hit)
        self.assertIn("setup: busybox:latest", hit["excerpt"])

    def test_the_running_digest_is_carried_into_the_excerpt(self):
        # The whole reason this finding is worth a pull request: the reviewer
        # can see the pin changes no bytes.
        hit = self.hit("gcr.io/acme/app:latest", pods=self.running(DIGEST_A))
        self.assertIn(f"currently running {DIGEST_A}", hit["excerpt"])
        self.assertEqual(hit["severity"], "minor")

    def test_a_workload_with_no_live_pod_says_so(self):
        hit = self.hit("gcr.io/acme/app:latest")
        self.assertIn("no running pod to read a digest from", hit["excerpt"])
        self.assertEqual(hit["severity"], "minor")

    def test_pods_split_across_digests_are_major_and_named(self):
        hit = self.hit("gcr.io/acme/app:latest", pods=self.running(DIGEST_A, DIGEST_B))
        self.assertEqual(hit["severity"], "major")
        self.assertIn("split across 2 digests", hit["excerpt"])
        self.assertIn(DIGEST_A, hit["excerpt"])
        self.assertIn(DIGEST_B, hit["excerpt"])
        self.assertIn("running different builds of app", hit["impact"])

    def test_two_revisions_mid_rollout_are_not_drift(self):
        # Old and new ReplicaSets each running their own digest is a rollout in
        # progress; only a split inside one revision proves the tag moved.
        pods = self.running(DIGEST_A, DIGEST_B)
        pods[0]["labels"] = {"pod-template-hash": "old"}
        pods[1]["labels"] = {"pod-template-hash": "new"}
        pods[0]["image_refs"] = {"app": "gcr.io/acme/app:v1"}
        pods[1]["image_refs"] = {"app": "gcr.io/acme/app:latest"}
        hit = self.hit("gcr.io/acme/app:latest", pods=pods)
        self.assertEqual(hit["severity"], "minor")
        self.assertIn("2 pod-template revisions live, one digest each", hit["excerpt"])
        self.assertNotIn("running different builds", hit["impact"])

    def test_two_revisions_writing_one_reference_are_drift(self):
        # A `rollout restart` or an env-only edit makes a second revision with
        # the same image string; two digests under it is the tag moving.
        pods = self.running(DIGEST_A, DIGEST_B)
        pods[0]["labels"] = {"pod-template-hash": "old"}
        pods[1]["labels"] = {"pod-template-hash": "new"}
        pods[0]["image_refs"] = pods[1]["image_refs"] = {"app": "gcr.io/acme/app:latest"}
        hit = self.hit("gcr.io/acme/app:latest", pods=pods)
        self.assertEqual(hit["severity"], "major")
        self.assertIn("all writing this reference, resolved to 2 digests", hit["excerpt"])
        self.assertIn("running different builds of app", hit["impact"])
        self.assertIn("a template change made a new revision", hit["impact"])
        self.assertNotIn("no deploy was made", hit["impact"])

    def test_a_template_reference_no_live_pod_runs_is_not_offered_as_a_pin(self):
        # `set image` on a paused Deployment or an OnDelete DaemonSet: the
        # running digest belongs to the old reference, so pinning the new one
        # to it would revert the change.
        pods = self.running(DIGEST_A)
        pods[0]["image_refs"] = {"app": "gcr.io/acme/app:v1"}
        hit = self.hit("gcr.io/acme/app:latest", pods=pods)
        self.assertEqual(hit["severity"], "minor")
        self.assertIn("no live pod runs this reference yet; they still write gcr.io/acme/app:v1", hit["excerpt"])
        self.assertNotIn("currently running", hit["excerpt"])

    def test_pods_that_report_no_digest_are_not_drift(self):
        # Pending or ImagePullBackOff pods write the reference and run nothing.
        pods = self.running(DIGEST_A, phase="Pending")
        pods[0]["images"] = {}
        pods[0]["image_refs"] = {"app": "gcr.io/acme/app:latest"}
        hit = self.hit("gcr.io/acme/app:latest", pods=pods)
        self.assertEqual(hit["severity"], "minor")
        self.assertIn("no running pod to read a digest from", hit["excerpt"])

    def test_a_webhook_appended_digest_is_the_same_reference(self):
        pods = self.running(DIGEST_A)
        pods[0]["image_refs"] = {"app": "gcr.io/acme/app:latest@sha256:" + "a" * 64}
        hit = self.hit("gcr.io/acme/app:latest", pods=pods)
        self.assertIn(f"currently running {DIGEST_A}", hit["excerpt"])

    def test_drift_between_restarted_revisions_is_seen_beside_an_older_rollout(self):
        # r1 still on :v1 from before the rollout; r2 and r3 both write
        # :latest and resolved it differently.
        pods = self.running("gcr.io/acme/app@sha256:" + "c" * 64, DIGEST_A, DIGEST_B)
        for pod, rev, ref in zip(pods, ("r1", "r2", "r3"), ("v1", "latest", "latest")):
            pod["labels"] = {"pod-template-hash": rev}
            pod["image_refs"] = {"app": f"gcr.io/acme/app:{ref}"}
        hit = self.hit("gcr.io/acme/app:latest", pods=pods)
        self.assertEqual(hit["severity"], "major")
        self.assertIn("2 pod-template revisions live, all writing this reference, resolved to 2 digests", hit["excerpt"])

    def test_a_split_under_a_stale_template_is_still_drift(self):
        pods = self.running(DIGEST_A, DIGEST_B)
        for pod in pods:
            pod["image_refs"] = {"app": "gcr.io/acme/app:v1"}
        hit = self.hit("gcr.io/acme/app:latest", pods=pods)
        self.assertEqual(hit["severity"], "major")
        self.assertIn("the pods still write gcr.io/acme/app:v1, not this reference", hit["excerpt"])

    def test_a_webhook_replacing_the_tag_with_a_digest_is_the_same_reference(self):
        pods = self.running(DIGEST_A)
        pods[0]["image_refs"] = {"app": "gcr.io/acme/app@sha256:" + "a" * 64}
        hit = self.hit("gcr.io/acme/app:latest", pods=pods)
        self.assertIn(f"currently running {DIGEST_A}", hit["excerpt"])

    def test_a_split_names_only_the_split_revisions_digests(self):
        old = "gcr.io/acme/app@sha256:" + "c" * 64
        pods = self.running(old, DIGEST_A, DIGEST_B)
        pods[0]["labels"] = {"pod-template-hash": "r1"}
        pods[1]["labels"] = pods[2]["labels"] = {"pod-template-hash": "r2"}
        hit = self.hit("gcr.io/acme/app:latest", pods=pods)
        self.assertIn("split across 2 digests", hit["excerpt"])
        self.assertNotIn(old, hit["excerpt"])

    def test_a_split_inside_one_revision_is_still_drift(self):
        pods = self.running(DIGEST_A, DIGEST_B, DIGEST_A)
        pods[0]["labels"] = pods[1]["labels"] = {"pod-template-hash": "new"}
        pods[2]["labels"] = {"pod-template-hash": "old"}
        self.assertEqual(self.hit("gcr.io/acme/app:latest", pods=pods)["severity"], "major")

    def test_a_finished_pod_does_not_contribute_a_digest(self):
        # A Succeeded Job pod's image is not what is running.
        hit = self.hit(
            "gcr.io/acme/app:latest",
            pods=self.running(DIGEST_A, phase="Succeeded") + self.running(DIGEST_B),
        )
        self.assertEqual(hit["severity"], "minor")
        self.assertIn(f"currently running {DIGEST_B}", hit["excerpt"])

    def test_another_workloads_pods_do_not_contribute_a_digest(self):
        self.assertIn(
            "no running pod",
            self.hit("gcr.io/acme/app:latest", pods=self.running(DIGEST_A, name="other"))["excerpt"],
        )

    def test_a_pod_in_another_namespace_does_not_contribute_a_digest(self):
        pods = self.running(DIGEST_A)
        pods[0]["ns"] = "elsewhere"
        self.assertIn("no running pod", self.hit("gcr.io/acme/app:latest", pods=pods)["excerpt"])


class TestPodImageRefs(unittest.TestCase):
    def test_every_container_and_init_container_reference_is_recorded(self):
        pod = {"spec": {"containers": [{"name": "app", "image": "gcr.io/acme/app:latest"}],
                        "initContainers": [{"name": "init", "image": "busybox"}]}}
        self.assertEqual(collect._pod_image_refs(pod), {"app": "gcr.io/acme/app:latest", "init": "busybox"})


class TestRunningDigests(unittest.TestCase):
    """Only a real registry digest reference is a pin worth writing."""

    def digests(self, image_id, name="app", init=False):
        key = "initContainerStatuses" if init else "containerStatuses"
        return collect._running_digests({"status": {key: [{"name": name, "imageID": image_id}]}})

    def test_a_bare_digest_reference_is_taken(self):
        self.assertEqual(self.digests(DIGEST_A), {"app": DIGEST_A})

    def test_the_docker_pullable_prefix_is_stripped(self):
        self.assertEqual(self.digests(f"docker-pullable://{DIGEST_A}"), {"app": DIGEST_A})

    def test_an_init_container_status_is_read(self):
        self.assertEqual(self.digests(DIGEST_A, name="setup", init=True), {"setup": DIGEST_A})

    def test_a_locally_loaded_image_is_dropped(self):
        # Pinning to one of these produces a manifest no other node can pull,
        # which is worse than the floating tag it replaced.
        for image_id in ("docker://sha256:" + "a" * 64, "sha256:" + "a" * 64, "", "app:latest"):
            with self.subTest(image_id=image_id):
                self.assertEqual(self.digests(image_id), {})

    def test_a_truncated_digest_is_dropped(self):
        self.assertEqual(self.digests("gcr.io/acme/app@sha256:abc123"), {})


class TestKccObjectWedged(unittest.TestCase):
    """§2.12. The check that audits the mechanism the other eleven assume."""

    # The message KCC wrote on both ComputeFirewalls for the four hours the
    # controller's service account was short of `compute.securityAdmin`.
    FORBIDDEN = (
        "Update call failed: error applying desired state: Error updating Firewall: "
        "googleapi: Error 403: Required 'compute.firewalls.update' permission for "
        "'projects/adamparco-kage/global/firewalls/default-allow-ssh', forbidden"
    )

    def run_on(self, *objects):
        return collect.check_kcc_object_wedged({"kcc_objects": list(objects)})

    def test_a_healthy_fleet_of_objects_reports_nothing(self):
        self.assertEqual(
            self.run_on(
                kcc_object("ComputeFirewall", "allow-ssh"),
                kcc_object("ContainerCluster", "prod-usc1"),
            ),
            [],
        )

    def test_no_ready_condition_is_not_a_finding(self):
        """The state KCC leaves an object in between the apply and its first
        reconcile. Reading it as wedged fires on every fresh commit, which is
        the fastest way to teach a reader to skip this check."""
        self.assertEqual(self.run_on(kcc_object("ComputeFirewall", "new", ready=None)), [])

    def test_a_denied_permission_is_one_critical_naming_the_object(self):
        hits = self.run_on(
            kcc_object("ComputeFirewall", "default-allow-ssh", ready="False",
                       reason="UpdateFailed", message=self.FORBIDDEN)
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "ComputeFirewall/default-allow-ssh")
        self.assertEqual(hits[0]["namespace"], "kubeagents-system")
        # The permission and the resource survive into the excerpt: they are
        # what tells the reader which role to grant, and the SOP's remediation
        # says to read the role off the permission rather than the kind.
        self.assertIn("compute.firewalls.update", hits[0]["excerpt"])
        self.assertIn("403", hits[0]["excerpt"])
        # Default severity -- no per-hit override on the cause arm.
        self.assertNotIn("severity", hits[0])

    def test_dependents_are_counted_onto_the_cause_not_emitted_alone(self):
        """The 2026-09-05 shape: one PubSubTopic short of `roles/pubsub.editor`,
        fifteen ContainerClusters stalled behind it. One finding, not sixteen."""
        clusters = [
            kcc_object("ContainerCluster", f"spoke-{i}", ready="False",
                       reason="DependencyNotReady",
                       message="reference PubSubTopic gke-upgrade-notifications is not ready")
            for i in range(15)
        ]
        hits = self.run_on(
            kcc_object("PubSubTopic", "gke-upgrade-notifications", ready="False",
                       reason="UpdateFailed", message="Error 403: permission denied on pubsub.topics.update"),
            *clusters,
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "PubSubTopic/gke-upgrade-notifications")
        self.assertIn("15 other Config Connector object(s)", hits[0]["excerpt"])
        # Named, but not all fifteen: a wall of names is not more informative
        # than three and a count.
        self.assertIn("+12 more", hits[0]["excerpt"])

    def test_a_missing_referent_is_a_cause_not_a_dependent(self):
        """`DependencyNotFound` is not `DependencyNotReady`.

        Both strings name a reference and both are set on the object holding
        it, so the two are easy to conflate -- and conflating them loses the
        finding. A dependent is folded into a count because repairing the
        object it waits on clears it; nothing clears a reference to something
        that does not exist except editing this object. Both messages here are
        verbatim from a live probe against the hub on 2026-09-06: the same
        `ComputeFirewall`, pointed first at an absent network and then at one
        that existed and had failed to create.
        """
        absent = self.run_on(
            kcc_object("ComputeFirewall", "kcc-wedge-probe", ready="False",
                       reason="DependencyNotFound",
                       message="reference ComputeNetwork kubeagents-system/"
                               "this-network-does-not-exist is not found"),
        )
        self.assertEqual([h["object"] for h in absent], ["ComputeFirewall/kcc-wedge-probe"])
        self.assertNotIn("other Config Connector object(s)", absent[0]["excerpt"])
        self.assertNotIn("severity", absent[0])

        # The same object one character of KCC vocabulary away, and now it is
        # the symptom it looks like: folded onto the network that is the cause.
        unready = self.run_on(
            kcc_object("ComputeNetwork", "kcc-wedge-probe-net", ready="False",
                       reason="UpdateFailed",
                       message="Update call failed: error applying desired state: "
                               "summary: Error creating Network: googleapi: Error 404"),
            kcc_object("ComputeFirewall", "kcc-wedge-probe", ready="False",
                       reason="DependencyNotReady",
                       message="reference ComputeNetwork kubeagents-system/"
                               "kcc-wedge-probe-net is not ready"),
        )
        self.assertEqual([h["object"] for h in unready], ["ComputeNetwork/kcc-wedge-probe-net"])
        self.assertIn("1 other Config Connector object(s)", unready[0]["excerpt"])

    def test_two_causes_each_get_their_own_finding(self):
        hits = self.run_on(
            kcc_object("ComputeFirewall", "allow-rdp", ready="False", reason="UpdateFailed", message="403"),
            kcc_object("ComputeFirewall", "allow-ssh", ready="False", reason="UpdateFailed", message="403"),
        )
        self.assertEqual(
            [h["object"] for h in hits],
            ["ComputeFirewall/allow-rdp", "ComputeFirewall/allow-ssh"],
        )
        # Neither counts the other as a dependent: both are causes.
        self.assertNotIn("other Config Connector object(s)", hits[0]["excerpt"])

    def test_dependents_with_no_cause_are_each_reported_at_minor(self):
        """Every unready object blaming a dependency, and no unready dependency
        on the cluster: the cause is a reference to something Config Connector
        does not own here, so there is nothing else to name and folding these
        into a count would report the problem to nobody."""
        hits = self.run_on(
            kcc_object("ComputeFirewall", "a", ready="False", reason="DependencyNotReady", message="waiting"),
            kcc_object("ComputeFirewall", "b", ready="False", reason="DependencyNotReady", message="waiting"),
        )
        self.assertEqual(len(hits), 2)
        for hit in hits:
            self.assertEqual(hit["severity"], "minor")
            self.assertIn("waiting on a dependency", hit["impact"])

    def test_a_long_upstream_error_is_truncated_not_pasted_whole(self):
        hits = self.run_on(
            kcc_object("ComputeFirewall", "x", ready="False", reason="UpdateFailed", message="E" * 4000)
        )
        self.assertLess(len(hits[0]["excerpt"]), 500)

    def test_ready_true_is_never_a_finding_whatever_else_it_says(self):
        """The inverse of the `kcc-does-not-revert-spec-absent-fields` error:
        `Ready=True` with a stale-looking reason is still an object KCC is
        applying, and flagging it would republish a fix that landed."""
        self.assertEqual(
            self.run_on(kcc_object("ComputeFirewall", "x", ready="True", reason="UpToDate")),
            [],
        )


def lb_service(
    name="db",
    ns="payments",
    ports=(5432,),
    svc_type="LoadBalancer",
    annotations=None,
    labels=None,
    source_ranges=None,
    ingress=("34.10.11.12",),
    protocol=None,
):
    svc = {
        "kind": "Service",
        "metadata": {"namespace": ns, "name": name, "annotations": annotations or {}, "labels": labels or {}},
        "spec": {
            "type": svc_type,
            "ports": [
                {"port": p, **({"protocol": protocol} if protocol else {})} for p in ports
            ],
        },
    }
    if source_ranges is not None:
        svc["spec"]["loadBalancerSourceRanges"] = list(source_ranges)
    # Omitted rather than emptied when `ingress` is None, for the reason
    # `ai_service` gives: a balancer still provisioning has no `ingress` key.
    if ingress is not None:
        svc["status"] = {"loadBalancer": {"ingress": [{"ip": addr} for addr in ingress]}}
    return svc


class TestLbWorldOpen(unittest.TestCase):
    def run_on(self, *services):
        return collect.check_lb_world_open({"services": list(services)})

    def test_a_world_open_postgres_port_is_flagged(self):
        hits = self.run_on(lb_service())
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "Service/db")
        self.assertEqual(hits[0]["namespace"], "payments")
        self.assertIn("5432/TCP (PostgreSQL)", hits[0]["excerpt"])

    def test_the_address_is_never_published(self):
        # The whole content of this vulnerability is where to connect, and
        # these findings are filed as comments on a public GitHub issue.
        # `adopt_collector_evidence` forces this excerpt over whatever the
        # model wrote, so the rule holds here or nowhere.
        hits = self.run_on(lb_service(ingress=("34.10.11.12",)))
        self.assertNotIn("34.10.11.12", hits[0]["excerpt"])
        self.assertIn("1 assigned address, none of them private", hits[0]["excerpt"])

    def test_a_web_port_is_not_flagged(self):
        # The exclusion that keeps the check usable: a LoadBalancer publishing
        # 80 or 443 is a LoadBalancer doing its job, and admitting them reports
        # every one in the fleet, daily.
        self.assertEqual(self.run_on(lb_service(ports=(80, 443, 8080))), [])

    def test_a_web_port_beside_a_management_port_is_still_flagged(self):
        hits = self.run_on(lb_service(ports=(443, 6379)))
        self.assertEqual(len(hits), 1)
        self.assertIn("6379/TCP (Redis)", hits[0]["excerpt"])
        self.assertNotIn("443", hits[0]["excerpt"])

    def test_every_management_port_is_named_in_order(self):
        hits = self.run_on(lb_service(ports=(27017, 22)))
        self.assertIn("22/TCP (SSH), 27017/TCP (MongoDB)", hits[0]["excerpt"])

    def test_a_clusterip_is_not_flagged(self):
        self.assertEqual(self.run_on(lb_service(svc_type="ClusterIP")), [])

    def test_a_nodeport_is_not_flagged(self):
        # GKE's node firewall does not admit a node port from outside by
        # default, so the Service alone does not put the port on the internet.
        self.assertEqual(self.run_on(lb_service(svc_type="NodePort")), [])

    def test_the_current_internal_lb_annotation_is_not_flagged(self):
        svc = lb_service(annotations={"networking.gke.io/load-balancer-type": "Internal"})
        self.assertEqual(self.run_on(svc), [])

    def test_the_legacy_internal_lb_annotation_is_not_flagged(self):
        svc = lb_service(annotations={"cloud.google.com/load-balancer-type": "Internal"})
        self.assertEqual(self.run_on(svc), [])

    def test_a_real_source_range_is_not_flagged(self):
        # GKE programs `loadBalancerSourceRanges` into the firewall in front of
        # the forwarding rule, so it is enforcement rather than intent.
        self.assertEqual(self.run_on(lb_service(source_ranges=["10.0.0.0/8"])), [])

    def test_a_default_route_source_range_restricts_nothing(self):
        hits = self.run_on(lb_service(source_ranges=["0.0.0.0/0"]))
        self.assertEqual(len(hits), 1)

    def test_an_unparseable_source_range_restricts_nothing(self):
        hits = self.run_on(lb_service(source_ranges=["not-a-cidr"]))
        self.assertEqual(len(hits), 1)

    def test_an_rfc1918_address_is_not_reachable_from_the_internet(self):
        self.assertEqual(self.run_on(lb_service(ingress=("10.150.0.78",))), [])

    def test_one_public_address_beside_a_private_one_is_still_flagged(self):
        hits = self.run_on(lb_service(ingress=("10.150.0.78", "34.10.11.12")))
        self.assertEqual(len(hits), 1)
        self.assertIn("2 assigned addresses, 1 of them not private", hits[0]["excerpt"])

    def test_a_balancer_with_no_address_yet_is_flagged_on_its_spec(self):
        hits = self.run_on(lb_service(ingress=None))
        self.assertEqual(len(hits), 1)
        self.assertIn("no address assigned yet", hits[0]["excerpt"])

    def test_a_udp_port_sharing_a_number_is_not_flagged(self):
        # None of these are UDP services; flagging one would be flagging the
        # number rather than the thing.
        self.assertEqual(self.run_on(lb_service(ports=(27017,), protocol="UDP")), [])

    def test_an_omitted_protocol_reads_as_tcp(self):
        self.assertEqual(len(self.run_on(lb_service(ports=(3306,)))), 1)

    def test_a_system_namespace_is_suppressed(self):
        self.assertEqual(self.run_on(lb_service(ns="kube-system")), [])

    def test_an_addon_managed_service_is_suppressed(self):
        svc = lb_service(labels={"addonmanager.kubernetes.io/mode": "Reconcile"})
        self.assertEqual(self.run_on(svc), [])

    def test_a_service_with_no_ports_is_not_flagged(self):
        self.assertEqual(self.run_on(lb_service(ports=())), [])


class TestComplianceCollectCluster(unittest.TestCase):
    """One end-to-end pass over compliance-audit's real collection plan --
    five distinct kubectl/gcloud commands, gated and cross-referenced --
    proving the multi-source builder actually composes with the shared
    check-iteration loop `collect_cluster` runs regardless of stream shape.
    """

    CLUSTER = {"name": "prod-usc1", "project": "acme", "location": "us-central1", "autopilot": False}

    # GKE's own answer when `node-pools list` is aimed at an Autopilot
    # cluster. The fake used to return rc=0 here whatever the cluster was,
    # which is why the gate failure this class is supposed to cover survived
    # a test named for exactly that case: a fake that answers every argv
    # successfully cannot tell a command the API runs from one it refuses.
    AUTOPILOT_NODE_POOLS_ERROR = (
        "ERROR: (gcloud.container.node-pools.list) ResponseError: code=400, "
        "message=Autopilot node pools cannot be accessed or modified."
    )

    # One in-scope Pod by default, hardened so it trips nothing. A cluster
    # with nothing in scope declares every workload-scoped check inapplicable
    # -- correct, and not what most of this class is about, so a test that
    # wants the empty-scope path asks for it with `workload_items=[]`.
    @staticmethod
    def benign_pod():
        pod = compliance_pod("app")
        pod["spec"]["containers"][0]["securityContext"] = {
            "runAsNonRoot": True,
            "allowPrivilegeEscalation": False,
            "seccompProfile": {"type": "RuntimeDefault"},
            "capabilities": {"drop": ["ALL"]},
        }
        return pod

    def run_with(self, workload_items=None, rbac_items=(), netpol_items=(), sa_items=(), describe=None, node_pools=(), ccnp_run=None, cluster=None, allowlist_items=None, kcc_items=None, svc_items=()):
        workload_items = [self.benign_pod()] if workload_items is None else workload_items
        describe = describe if describe is not None else {}
        target = cluster or self.CLUSTER
        self.issued = []

        def run(argv, **kwargs):
            self.issued.append(list(argv))
            if "get-credentials" in argv:
                return Run(argv, 0, "", "", 0.05)
            if argv[:2] == ["kubectl", "get"]:
                kinds = argv[2]
                if kinds == collect.COMPLIANCE_DUMP_KINDS:
                    return Run(argv, 0, json.dumps(dump_of(*workload_items)), "", 0.1)
                if "clusterroles" in kinds:
                    return Run(argv, 0, json.dumps(dump_of(*rbac_items)), "", 0.1)
                if kinds == "netpol,ns":
                    return Run(argv, 0, json.dumps(dump_of(*netpol_items)), "", 0.1)
                if kinds == "sa":
                    return Run(argv, 0, json.dumps(dump_of(*sa_items)), "", 0.1)
                if kinds == "svc":
                    return Run(argv, 0, json.dumps(dump_of(*svc_items)), "", 0.1)
                if kinds == "ccnp":
                    if ccnp_run is not None:
                        return ccnp_run
                    # A cluster without Dataplane V2's CRD, and the default:
                    # only this answer reads as "no cluster-wide policies".
                    return Run(argv, 1, "", 'error: the server doesn\'t have a resource type "ccnp"', 0.05)
                if kinds == collect.KCC_CATEGORY:
                    if kcc_items is None:
                        # A cluster not running Config Connector, which is
                        # every cluster in a fleet but the one hosting it, and
                        # so the default here.
                        return Run(argv, 1, "", 'error: the server doesn\'t have a resource type "gcp"', 0.05)
                    # A callable answers the read itself -- for the malformed
                    # responses `run_and_gate` cannot distinguish from an
                    # absent CRD, which no list of objects can express.
                    if callable(kcc_items):
                        return kcc_items(argv, **kwargs)
                    return Run(argv, 0, json.dumps(dump_of(*kcc_items)), "", 0.05)
                if kinds in collect.AUTOPILOT_ALLOWLIST_KINDS:
                    if callable(allowlist_items):
                        return allowlist_items(argv, **kwargs)
                    if allowlist_items is None or kinds != collect.AUTOPILOT_ALLOWLIST_KINDS[0]:
                        # What a cluster without the CRD answers, which is
                        # every Standard cluster and the default here. A list
                        # answers the first kind alone.
                        return Run(argv, 1, "", f'error: the server doesn\'t have a resource type "{kinds}"', 0.05)
                    return Run(argv, 0, json.dumps(dump_of(*allowlist_items)), "", 0.05)
            if argv[:3] == ["gcloud", "container", "clusters"]:
                return Run(argv, 0, json.dumps(describe), "", 0.1)
            if argv[:3] == ["gcloud", "container", "node-pools"]:
                if target.get("autopilot"):
                    return Run(argv, 1, "", self.AUTOPILOT_NODE_POOLS_ERROR, 0.1)
                return Run(argv, 0, json.dumps(list(node_pools)), "", 0.1)
            return Run(argv, 0, "", "", 0.01)

        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), patch.object(collect, "SCRATCH_DIR", tmp):
                return collect.collect_cluster(target, "compliance-audit", collect.COMPLIANCE_CHECKS, run=run)

    def test_a_clean_cluster_reports_nothing(self):
        result = self.run_with(
            describe={
                "workloadIdentityConfig": {"workloadPool": "acme.svc.id.goog"},
                "privateClusterConfig": {"enablePrivateEndpoint": True},
            }
        )
        self.assertEqual(result["outcome"], "collected")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(len(result["commands"]), 15)

    def test_a_dirty_cluster_reports_across_multiple_sources(self):
        privileged_pod = compliance_pod("bad")
        privileged_pod["spec"]["containers"][0]["securityContext"] = {"privileged": True}
        result = self.run_with(
            workload_items=[privileged_pod],
            rbac_items=[
                crb("admin-binding", [subject("ServiceAccount", "app", "default")]),
            ],
            netpol_items=[namespace("default")],
            describe={"workloadIdentityConfig": {}},
        )
        slugs = {c["check"] for c in result["candidates"]}
        self.assertIn("privileged-container", slugs)
        self.assertIn("cluster-admin-binding", slugs)
        self.assertIn("netpol-missing", slugs)
        self.assertIn("workload-identity-off", slugs)

    def test_a_gate_failure_on_one_source_fails_the_whole_cluster(self):
        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return Run(argv, 0, "", "", 0.05)
            if argv[:2] == ["kubectl", "get"] and argv[2] == collect.COMPLIANCE_DUMP_KINDS:
                return Run(argv, 0, json.dumps(dump_of()), "", 0.1)
            if argv[:2] == ["kubectl", "get"] and "clusterroles" in argv[2]:
                return Run(argv, 1, "", "RBAC forbidden", 0.1)  # this one fails
            return Run(argv, 0, "", "", 0.01)

        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), patch.object(collect, "SCRATCH_DIR", tmp):
                result = collect.collect_cluster(self.CLUSTER, "compliance-audit", collect.COMPLIANCE_CHECKS, run=run)
        self.assertEqual(result["outcome"], "gate-failed")
        self.assertNotIn("candidates", result)

    def test_a_cluster_network_policy_suppresses_netpol_missing(self):
        result = self.run_with(
            workload_items=[compliance_pod("api", ns="payments")],
            netpol_items=[namespace("payments")],
            ccnp_run=Run(["x"], 0, json.dumps(dump_of(ccnp("fleet-wide"))), "", 0.1),
        )
        self.assertNotIn("netpol-missing", {c["check"] for c in result["candidates"]})

    def test_a_namespace_holding_only_finished_pods_is_not_flagged_by_netpol_missing(self):
        # A Succeeded Job pod accepts no traffic, so it is no exposure.
        pod = compliance_pod("migrate", ns="payments")
        pod["status"] = {"phase": "Succeeded"}
        result = self.run_with(workload_items=[pod], netpol_items=[namespace("payments")])
        self.assertNotIn("netpol-missing", {c["check"] for c in result["candidates"]})

    def test_ccnp_read_failure_does_not_gate_the_cluster_closed(self):
        """Unlike RBAC/netpol/workload dumps, a missing `ccnp` CRD is the
        common case (Dataplane V2's ClusterNetworkPolicy is not installed on
        every cluster) -- it must degrade to "no cluster-wide policies seen"
        rather than failing the whole cluster the way a real input gap does."""
        result = self.run_with(
            workload_items=[compliance_pod("api", ns="payments")],
            netpol_items=[namespace("payments")],
            ccnp_run=Run(["x"], 1, "", "the server doesn't have a resource type \"ccnp\"", 0.01),
        )
        self.assertEqual(result["outcome"], "collected")
        self.assertIn("netpol-missing", {c["check"] for c in result["candidates"]})

    def test_autopilot_pre_fills_checks_not_applicable_and_excludes_them_from_commands(self):
        autopilot_cluster = {**self.CLUSTER, "autopilot": True}
        result = self.run_with(cluster=autopilot_cluster)
        not_applicable_slugs = {e["check"] for e in result["checks_not_applicable"]}
        self.assertEqual(
            not_applicable_slugs,
            # `kcc-object-wedged` rides along on every cluster in this fixture:
            # the fake answers `kubectl get gcp` the way a cluster without
            # Config Connector does. It is declared by the collector rather
            # than by the Autopilot table, so it is not part of what this test
            # is about -- see `test_a_cluster_without_config_connector_...`.
            {"privileged-container", "host-namespace", "hostpath-mount", "kcc-object-wedged"},
        )
        command_slugs = {c["check"] for c in result["commands"]}
        self.assertFalse(not_applicable_slugs & command_slugs)
        # Every reason is the SOP's own canonical text, not a placeholder.
        for entry in result["checks_not_applicable"]:
            if entry["check"] == "kcc-object-wedged":
                continue
            self.assertIn("Autopilot", entry["reason"])

    def test_no_reason_claims_the_object_cannot_exist_on_the_cluster(self):
        """The reasons used to read "privileged containers are rejected at
        admission and cannot exist here", and privileged containers, host
        namespaces and hostPath mounts all run on this fleet's Autopilot
        clusters -- as Google's own kube-system add-ons, which the universal
        suppressions drop. A reason a reviewer can disprove by pointing at a
        running pod is worse than no reason: it is published verbatim in the
        ledger under _Not applicable_ as a statement about the cluster."""
        result = self.run_with(cluster={**self.CLUSTER, "autopilot": True})
        for entry in result["checks_not_applicable"]:
            # This half binds every reason on the manifest, whichever cause
            # wrote it: an unfalsifiable claim is the defect, not Autopilot's
            # phrasing of one.
            self.assertNotIn("cannot exist", entry["reason"])
            if entry["check"] == "kcc-object-wedged":
                continue
            # Scoped to the workloads the audit can see, and to the admission
            # policy holding -- both halves, on every Autopilot reason.
            self.assertIn("in-scope workloads", entry["reason"])
            self.assertIn("WorkloadAllowlist", entry["reason"])

    def test_an_installed_workload_allowlist_withdraws_every_declaration(self):
        """Each reason ends by saying the cluster carries no WorkloadAllowlist,
        because Autopilot's admission rule is a policy one lifts
        (`autogke-disallow-privilege`, `autogke-no-write-mode-hostpath`). One
        existing falsifies all three at once, whether or not any check fired,
        so the collector reads for them rather than asserting their absence."""
        result = self.run_with(
            cluster={**self.CLUSTER, "autopilot": True},
            allowlist_items=[{"kind": "WorkloadAllowlist", "metadata": {"name": "vendor-agent"}}],
        )
        self.assertEqual(
            {e["check"] for e in result["checks_not_applicable"]}, {"kcc-object-wedged"}
        )
        # And they report as what they are: checks that ran and found nothing.
        command_slugs = {c["check"] for c in result["commands"]}
        self.assertLessEqual(
            {"privileged-container", "host-namespace", "hostpath-mount"}, command_slugs
        )
        self.assertEqual(len(command_slugs), 15)

    def test_an_absent_allowlist_crd_reads_as_no_allowlists(self):
        """The read is best-effort for the reason the `ccnp` one is: the CRDs
        come from Autopilot itself, so "the server doesn't have a resource
        type" is the answer and not a missing input. Failing the cluster
        closed on it would cost eleven checks to learn there are no
        exemptions."""
        result = self.run_with(cluster={**self.CLUSTER, "autopilot": True}, allowlist_items=None)
        self.assertEqual(result["outcome"], "collected")
        self.assertEqual(
            {e["check"] for e in result["checks_not_applicable"]},
            {"privileged-container", "host-namespace", "hostpath-mount", "kcc-object-wedged"},
        )

    def test_a_standard_cluster_never_reads_for_allowlists(self):
        """The CRDs only exist on Autopilot and the table only applies there,
        so the read has nothing to inform on a Standard cluster."""
        self.run_with()
        self.assertEqual([a for a in self.issued if a[:2] == ["kubectl", "get"] and a[2] in collect.AUTOPILOT_ALLOWLIST_KINDS], [])

    def test_an_unreadable_allowlist_type_withdraws_every_declaration(self):
        """kubectl resolves every type of a multi-type read before listing any,
        so the read is one call per type. One type that fails for a reason
        other than being unserved -- Forbidden here -- leaves the allowlist
        state unknown, and every reason asserts no allowlist exists."""
        def answer(argv, **kwargs):
            if argv[2] == collect.AUTOPILOT_ALLOWLIST_KINDS[-1]:
                return Run(argv, 1, "", 'Error from server (Forbidden): allowlistedv2workloads.auto.gke.io is forbidden', 0.05)
            return Run(argv, 0, json.dumps(dump_of()), "", 0.05)

        result = self.run_with(cluster={**self.CLUSTER, "autopilot": True}, allowlist_items=answer)
        self.assertEqual(result["outcome"], "collected")
        self.assertEqual({e["check"] for e in result["checks_not_applicable"]}, {"kcc-object-wedged"})
        self.assertLessEqual(
            {"privileged-container", "host-namespace", "hostpath-mount"},
            {c["check"] for c in result["commands"]},
        )

    def test_an_unserved_allowlist_type_beside_served_empty_ones_still_declares(self):
        """A cluster on a GKE version serving some of the four CRDs: the
        unserved one is the answer for its kind, the served ones list nothing,
        and the declarations stand."""
        def answer(argv, **kwargs):
            if argv[2] == collect.AUTOPILOT_ALLOWLIST_KINDS[-1]:
                return Run(argv, 1, "", f'error: the server doesn\'t have a resource type "{argv[2]}"', 0.05)
            return Run(argv, 0, json.dumps(dump_of()), "", 0.05)

        result = self.run_with(cluster={**self.CLUSTER, "autopilot": True}, allowlist_items=answer)
        self.assertEqual(
            {e["check"] for e in result["checks_not_applicable"]},
            {"privileged-container", "host-namespace", "hostpath-mount", "kcc-object-wedged"},
        )

    def test_standard_cluster_declares_only_the_config_connector_check(self):
        """A Standard cluster with in-scope workloads rules out nothing the
        Autopilot table or the empty-scope rule would rule out. What it does
        rule out is `kcc-object-wedged`: the API server there does not serve
        the `gcp` category at all. Without that entry the check would count
        against the cluster's coverage denominator on every cluster in the
        fleet but the one hosting Config Connector -- a permanent 11/12 on
        fifteen clusters, which is the reverse failure of a false all-clear
        and just as good at teaching a reader to ignore the column."""
        result = self.run_with()
        self.assertEqual(
            {e["check"] for e in result["checks_not_applicable"]}, {"kcc-object-wedged"}
        )

    def kcc_reason(self, result):
        # Absence is a not-applicable reason; a failed read is an unevaluated
        # one. Either way the reason text is what these tests grade.
        for entry in (result.get("checks_not_applicable") or []) + (result.get("checks_unevaluated") or []):
            if entry["check"] == "kcc-object-wedged":
                return entry["reason"]
        return None

    def test_a_cluster_without_config_connector_says_so_and_says_why(self):
        result = self.run_with()
        reason = self.kcc_reason(result)
        self.assertIn("not installed on this cluster", reason)
        self.assertNotIn("kcc-object-wedged", {c["check"] for c in result["commands"]})

    def test_a_cluster_running_config_connector_runs_the_check(self):
        result = self.run_with(kcc_items=[kcc_object("ComputeFirewall", "allow-ssh")])
        self.assertIsNone(self.kcc_reason(result))
        entry = next(c for c in result["commands"] if c["check"] == "kcc-object-wedged")
        self.assertIn("get gcp -A", entry["command"])
        self.assertEqual(entry["rc"], 0)

    def test_config_connector_installed_but_declaring_nothing_is_its_own_reason(self):
        """Distinct from "not installed": the controller is here and the check
        genuinely ran out of subjects, which is a different statement about the
        cluster and the one a reviewer can check."""
        result = self.run_with(kcc_items=[])
        reason = self.kcc_reason(result)
        self.assertIn("declares no GCP resources", reason)
        self.assertNotIn("not installed", reason)

    def test_a_forbidden_gcp_read_is_undetermined_not_absence(self):
        """Only kubectl's "doesn't have a resource type" says the category is
        unserved. A Forbidden, or one CRD in the category refusing to list,
        also exits non-zero, and on the hub it would otherwise publish that
        Config Connector is not installed where it is."""
        result = self.run_with(
            kcc_items=lambda argv, **kw: Run(argv, 1, "", "Error from server (Forbidden): computefirewalls.compute.cnrm.cloud.google.com is forbidden", 0.1)
        )
        reason = self.kcc_reason(result)
        self.assertIn("Undetermined", reason)
        self.assertIn("Forbidden", reason)
        self.assertIn("cleared nothing", reason)
        self.assertNotIn("not installed", reason)

    def test_a_timed_out_gcp_read_is_undetermined_not_absence(self):
        """The live defect, 2026-09-06, on the first day this check shipped.

        `kubectl get gcp -A` is a *category* read: it expands to every CRD
        Config Connector installs -- 221 on this fleet's hub -- and lists
        against each. That makes it slow precisely where Config Connector is
        installed and instant everywhere else, so the cluster the check exists
        for is the only one that can time out. It did, at 60s under the
        parallel sweep, and `rc != 0` sent it down the not-installed arm: the
        ledger published "Config Connector is not installed on this cluster"
        about the one cluster in the fleet running Config Connector, and cited
        exit 124 as the API server's answer for a type it does not serve. 124
        is this collector's own marker for having given up.
        """
        result = self.run_with(kcc_items=lambda argv, **kw: Run(argv, collect.TIMEOUT_RC, "", "", 60.0))
        reason = self.kcc_reason(result)
        self.assertIn("Undetermined", reason)
        self.assertIn("did not finish", reason)
        self.assertIn("cleared nothing", reason)
        self.assertNotIn("not installed", reason)
        # The false claim's own words, so a reintroduction fails here rather
        # than in a report.
        self.assertNotIn("does not serve", reason)

    def test_the_gcp_read_gets_the_longer_timeout_and_the_others_do_not(self):
        """A truthful `Undetermined` still leaves the check having never run on
        the only cluster it can apply to, so the timeout is the other half of
        the fix. Asserted per-read, because `run_and_gate` takes a default and
        the whole defect was one read needing something other than it."""
        seen = {}

        def record(argv, **kwargs):
            seen["kcc"] = kwargs.get("timeout")
            return Run(argv, 0, json.dumps(dump_of(kcc_object("ComputeFirewall", "allow-ssh"))), "", 0.05)

        original = collect.run_and_gate
        observed = []

        def spy(argv, kubeconfig, **kwargs):
            observed.append((argv[2] if argv[:2] == ["kubectl", "get"] else argv[0], kwargs.get("timeout")))
            return original(argv, kubeconfig, **kwargs)

        with patch.object(collect, "run_and_gate", spy):
            self.run_with(kcc_items=record)
        self.assertEqual(seen["kcc"], collect.KCC_READ_TIMEOUT_S)
        self.assertGreater(collect.KCC_READ_TIMEOUT_S, collect.DEFAULT_TIMEOUT_S)
        others = [t for kind, t in observed if kind != collect.KCC_CATEGORY]
        self.assertTrue(others, "the spy saw no other reads, so it proves nothing about them")
        self.assertTrue(
            all(t in (None, collect.DEFAULT_TIMEOUT_S) for t in others),
            f"a read other than the `gcp` one took a custom timeout: {observed}",
        )

    def test_an_unreadable_gcp_response_never_claims_config_connector_is_absent(self):
        """`run_and_gate` collapses five different failures into `None` -- a
        non-zero exit, a timeout, empty stdout, unparseable JSON, and a `get`
        whose `.items` is not a list. Only the first means "no Config Connector
        here". Reading the others as that publishes "Config Connector is
        not installed on this cluster" in the ledger, under the audit's own
        name, on the strength of a read that failed -- a claim about the
        cluster that the collector has no evidence for and that hides a real
        wedge behind a clean-looking exclusion."""

        def truncated(argv, **kwargs):
            return Run(argv, 0, '{"items": [{"kind": "Compute', "", 0.05)

        for label, fake in (
            ("unparseable", truncated),
            ("empty stdout", lambda argv, **kw: Run(argv, 0, "", "", 0.05)),
            ("items not a list", lambda argv, **kw: Run(argv, 0, '{"items": {}}', "", 0.05)),
        ):
            with self.subTest(case=label):
                result = self.run_with(kcc_items=fake)
                reason = self.kcc_reason(result)
                self.assertIn("Undetermined", reason)
                self.assertNotIn("not installed", reason)
                self.assertIn("cleared nothing", reason)

    def test_a_check_that_found_something_is_not_also_declared_inapplicable(self):
        """The filter reached `commands` and not `candidates`, so a privileged
        pod on an Autopilot cluster produced a manifest saying both that the
        check cannot apply here and that it fired — with no `commands` entry
        behind the candidate. Each reason asserts the object cannot exist, so a
        candidate falsifies the reason rather than the finding."""
        privileged = compliance_pod("legacy", ns="payments")
        privileged["spec"]["containers"][0]["securityContext"] = {"privileged": True}
        result = self.run_with(workload_items=[privileged], cluster={**self.CLUSTER, "autopilot": True})
        not_applicable = {e["check"] for e in result.get("checks_not_applicable") or []}
        found = {c["check"] for c in result["candidates"]}
        self.assertIn("privileged-container", found)
        self.assertNotIn("privileged-container", not_applicable)
        self.assertIn("privileged-container", {c["check"] for c in result["commands"]})
        # Nor are the other two, though they found nothing: all three rest on
        # one admission rule, and a privileged pod says it is not holding on
        # this cluster, so a hostPath or host-namespace pod could be there too.
        self.assertEqual(not_applicable, {"kcc-object-wedged"})
        self.assertLessEqual({"host-namespace", "hostpath-mount"}, {c["check"] for c in result["commands"]})

    def test_autopilot_collects_rather_than_gate_failing_on_a_read_the_api_refuses(self):
        """The live regression: `node-pools list` 400s on Autopilot, and it was
        the last read in the compliance collector, so eleven checks that had
        already succeeded were discarded on the twelfth -- which could not have
        run. Three of the four clusters in the validation fleet are Autopilot,
        so every daily run of this stream collected one and gate-failed the
        rest."""
        result = self.run_with(cluster={**self.CLUSTER, "autopilot": True})
        self.assertEqual(result["outcome"], "collected")
        self.assertNotIn("error", result)

    def test_autopilot_never_issues_the_node_pools_read_at_all(self):
        """Not merely tolerating the failure -- not making the call. The check
        it backs is already declared inapplicable here, so the read has nothing
        to inform, and issuing it spends a round trip to be told 400."""
        self.run_with(cluster={**self.CLUSTER, "autopilot": True})
        self.assertEqual([a for a in self.issued if a[:3] == ["gcloud", "container", "node-pools"]], [])

    def test_a_standard_cluster_still_issues_it(self):
        self.run_with()
        self.assertTrue([a for a in self.issued if a[:3] == ["gcloud", "container", "node-pools"]])

    def test_a_standard_cluster_still_gate_fails_when_node_pools_fails(self):
        """The skip is Autopilot-specific. On a Standard cluster the read backs
        a check that genuinely applies, so losing it still fails closed."""

        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return Run(argv, 0, "", "", 0.05)
            if argv[:2] == ["kubectl", "get"]:
                return Run(argv, 0, json.dumps(dump_of()), "", 0.1)
            if argv[:3] == ["gcloud", "container", "clusters"]:
                return Run(argv, 0, json.dumps({}), "", 0.1)
            if argv[:3] == ["gcloud", "container", "node-pools"]:
                return Run(argv, 1, "", "permission denied", 0.1)
            return Run(argv, 0, "", "", 0.01)

        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), patch.object(collect, "SCRATCH_DIR", tmp):
                result = collect.collect_cluster(self.CLUSTER, "compliance-audit", collect.COMPLIANCE_CHECKS, run=run)
        self.assertEqual(result["outcome"], "gate-failed")
        self.assertIn("node-pools", result["error"])

    def test_autopilot_records_every_check_it_could_run(self):
        """The point of not gate-failing: the other checks reach the manifest.
        Sixteen checks minus the three Autopilot rules out and the one no
        cluster without Config Connector can run is twelve, each with a command
        behind it, and the four dispositioned rather than silently absent --
        absent is what §6 reads as a coverage gap."""
        result = self.run_with(cluster={**self.CLUSTER, "autopilot": True})
        command_slugs = {c["check"] for c in result["commands"]}
        na_slugs = {e["check"] for e in result["checks_not_applicable"]}
        self.assertEqual(len(command_slugs), 12)
        self.assertEqual(len(na_slugs), 4)
        self.assertFalse(command_slugs & na_slugs)
        self.assertTrue(all(c["rc"] == 0 for c in result["commands"]))

    def test_legacy_metadata_runs_on_autopilot_off_the_describe(self):
        """It used to be declared inapplicable for "no user-managed node pools
        to carry a metadata setting" -- false on a cluster with five pools that
        each carry `config.workloadMetadataConfig`. What Autopilot refuses is
        `node-pools list` (HTTP 400), not the pools existing, and `clusters
        describe` returns the same field. So the check runs, and reports the
        honest result: every Google-managed pool is on GKE_METADATA."""
        result = self.run_with(
            cluster={**self.CLUSTER, "autopilot": True},
            describe={
                "nodePools": [
                    {"name": "default-pool", "config": {"workloadMetadataConfig": {"mode": "GKE_METADATA"}}},
                    {"name": "pool-1", "config": {"workloadMetadataConfig": {"mode": "GKE_METADATA"}}},
                ]
            },
        )
        na_slugs = {e["check"] for e in result["checks_not_applicable"]}
        self.assertNotIn("legacy-metadata", na_slugs)
        command = next(c for c in result["commands"] if c["check"] == "legacy-metadata")
        self.assertIn("clusters describe", command["command"])
        self.assertNotIn("legacy-metadata", {c["check"] for c in result["candidates"]})

    def test_a_non_compliant_autopilot_pool_is_now_reachable(self):
        """The guard the hardcoded `node_pools = []` disarmed. With the pools
        coming from the describe, a pool off GKE_METADATA produces a candidate
        instead of being unreportable by construction on every run forever."""
        result = self.run_with(
            cluster={**self.CLUSTER, "autopilot": True},
            describe={
                "nodePools": [
                    {"name": "pool-1", "config": {"workloadMetadataConfig": {"mode": "GCE_METADATA"}}},
                ]
            },
        )
        candidate = next(c for c in result["candidates"] if c["check"] == "legacy-metadata")
        self.assertEqual(candidate["object"], "NodePool/pool-1")
        self.assertIn("GCE_METADATA", candidate["excerpt"])

    def test_the_describe_projection_asks_for_the_node_pool_fields(self):
        """`check_legacy_metadata` reads two fields off each pool. If the
        projection stops requesting them the check silently passes every pool
        on every Autopilot cluster, which is the failure this replaced."""
        self.run_with(cluster={**self.CLUSTER, "autopilot": True})
        describe = next(a for a in self.issued if a[:4] == ["gcloud", "container", "clusters", "describe"])
        fmt = describe[describe.index("--format") + 1]
        self.assertIn("nodePools[].name", fmt)
        self.assertIn("nodePools[].config.workloadMetadataConfig", fmt)

    def test_every_not_applicable_slug_has_a_detection_fed_real_input(self):
        """The invariant the `legacy-metadata` entry broke. The guard's whole
        safety property is that each detection runs anyway and withholds the
        declaration if it fires; a slug whose input is hardcoded empty can
        never fire, so the guard silently covers everything but it."""
        na_slugs = {slug for slug, _ in collect._COMPLIANCE_AUTOPILOT_NOT_APPLICABLE}
        workload_kinds = {
            spec.slug for spec in collect.COMPLIANCE_CHECKS if spec.kind == "workload"
        }
        self.assertLessEqual(na_slugs, workload_kinds)


# --------------------------------------------------------------------------- #
# ai-security-audit
# --------------------------------------------------------------------------- #


def ai_workload(kind="Deployment", name="vllm-llama", ns="default", image="acme/vllm:v1", container=None, pod_labels=None, volumes=None):
    """A workload whose default image trips the §2 AI discriminator on its
    own — every test that does not care about the discriminator itself can
    ignore that and focus on its own check."""
    c = {"name": "server", "image": image}
    if container:
        c.update(container)
    pod_spec = {"containers": [c]}
    if volumes is not None:
        pod_spec["volumes"] = volumes
    labels = pod_labels if pod_labels is not None else {"app": name}
    if kind == "Pod":
        return {"kind": "Pod", "metadata": {"namespace": ns, "name": name, "labels": labels}, "spec": pod_spec}
    template = {"metadata": {"labels": labels}, "spec": pod_spec}
    spec = {"jobTemplate": {"spec": {"template": template}}} if kind == "CronJob" else {"template": template}
    return {"kind": kind, "metadata": {"namespace": ns, "name": name, "labels": {}}, "spec": spec}


def ai_service(name, ns="default", selector=None, svc_type="LoadBalancer", annotations=None, ingress=None):
    svc = {
        "kind": "Service",
        "metadata": {"namespace": ns, "name": name, "annotations": annotations or {}},
        "spec": {"type": svc_type, "selector": selector if selector is not None else {"app": name}},
    }
    # Omitted entirely rather than left empty when `ingress` is None: a load
    # balancer that has not been assigned an address yet has no `ingress` key,
    # and that is the case the check falls back to annotations for.
    if ingress is not None:
        svc["status"] = {"loadBalancer": {"ingress": [{"ip": addr} for addr in ingress]}}
    return svc


class TestIsAiWorkload(unittest.TestCase):
    def test_a_known_model_server_image_matches(self):
        self.assertTrue(collect._is_ai_workload({"containers": [{"image": "docker.io/vllm/vllm-openai:v0.6"}]}))

    def test_an_unrelated_image_does_not_match(self):
        self.assertFalse(collect._is_ai_workload({"containers": [{"image": "nginx:1.25"}]}))

    def test_a_gpu_request_matches_regardless_of_image(self):
        spec = {"containers": [{"image": "acme/recommender:v4", "resources": {"limits": {"nvidia.com/gpu": "1"}}}]}
        self.assertTrue(collect._is_ai_workload(spec))

    def test_a_tpu_request_matches(self):
        spec = {"containers": [{"image": "acme/recommender:v4", "resources": {"limits": {"google.com/tpu": "1"}}}]}
        self.assertTrue(collect._is_ai_workload(spec))

    def test_a_node_label_style_tpu_key_does_not_match_a_resource_limit(self):
        # cloud.google.com/tpu-accelerator is a nodeSelector value, never a
        # resources.limits key -- the SOP is explicit this must not match.
        spec = {"containers": [{"image": "acme/x", "resources": {"limits": {}}}]}
        self.assertFalse(collect._is_ai_workload(spec))

    def test_the_cpu_served_serving_runtimes_match(self):
        """The image prong exists for the model server that requests no
        accelerator, so a runtime missing from it is invisible to the whole
        stream -- the cluster is reported clean because nothing was found to
        audit. `text-embeddings-inference` is the one that mattered: HuggingFace
        ships it alongside `text-generation-inference`, which was already
        listed, and it is routinely served on CPU."""
        for image in (
            "ghcr.io/huggingface/text-embeddings-inference:cpu-1.5",
            "openmmlab/lmdeploy:v0.6.1",
            "xprobe/xinference:v0.16.3",
            "quay.io/go-skynet/localai:v2.20.1",
            "bentoml/openllm@sha256:" + "0" * 64,
        ):
            with self.subTest(image=image):
                self.assertTrue(collect._is_ai_workload({"containers": [{"image": image}]}))

    def test_a_model_proxy_and_a_language_toolchain_stay_out_of_the_image_prong(self):
        """Both would be false positives of the shape the SOP's "never widen on
        naming" rule is about. `litellm` fronts model servers without holding a
        model, and this fleet runs one in `kubeagents-system`; `nim` was left
        off the list because these anchors cannot tell NVIDIA's inference
        containers from the Nim compiler.

        The image prong is what is asserted here, and litellm is still outside
        it. The real one carries three provider credentials and so is in scope
        through the credential prong instead -- see
        `test_a_named_provider_credential_matches`. A name it is not is the
        wrong reason to audit something; a credential it holds is a fact about
        it."""
        for image in (
            "us-east4-docker.pkg.dev/adamparco-kage/kube-agents/litellm:v1.96.2",
            "nimlang/nim:2.0",
        ):
            with self.subTest(image=image):
                self.assertFalse(collect._is_ai_workload({"containers": [{"image": image}]}))

    def test_a_named_provider_credential_matches(self):
        """The prong that does not depend on recognising a name. Every one of
        these is a container talking to a model provider under an image no
        allowlist has, which is the false negative that reports a cluster
        clean."""
        for env_name in (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GEMINI_API_KEY",
            "COHERE_API_KEY",
            "MISTRAL_API_KEY",
            "HF_TOKEN",
            "HUGGING_FACE_HUB_TOKEN",
            "REPLICATE_API_TOKEN",
        ):
            with self.subTest(env_name=env_name):
                spec = {"containers": [{"image": "acme/app:v1", "env": [{"name": env_name, "value": "x"}]}]}
                self.assertTrue(collect._is_ai_workload(spec))

    def test_a_secret_backed_provider_credential_still_matches(self):
        """Holding the credential is what puts a workload in scope; holding it
        correctly is 3.5's separate question. Reading the value here would put
        exactly the workloads that got it right out of the audit's reach --
        including this fleet's own litellm, whose three keys are all
        `secretKeyRef` and whose ClusterIP Service is a 3.1 verdict the stream
        could not otherwise reach."""
        spec = {"containers": [{
            "image": "us-east4-docker.pkg.dev/adamparco-kage/kube-agents/litellm:v1.96.2",
            "env": [
                {"name": "VERTEXAI_PROJECT", "value": "adamparco-kage"},
                {"name": "ANTHROPIC_API_KEY", "valueFrom": {"secretKeyRef": {"name": "k", "key": "k"}}},
            ],
        }]}
        self.assertTrue(collect._is_ai_workload(spec))

    def test_a_credential_shaped_name_that_names_no_provider_stays_out(self):
        """The scope prong is anchored whole-name where 3.5's is a substring
        heuristic, and this is the reason for the difference: admitting a
        workload subjects it to all six checks, including being asked whether
        its endpoint is a public *inference* endpoint. A CI runner holding a
        registry password is not a model server."""
        for env_name in (
            "REGISTRY_PASSWORD",
            "MODEL_REGISTRY_KEY_ID",
            "MY_OPENAI_API_KEY_BACKUP",
            "OPENAI_API_KEY_FILE",
            "OPENAI_ORG",
        ):
            with self.subTest(env_name=env_name):
                spec = {"containers": [{"image": "acme/ci:v1", "env": [{"name": env_name, "value": "x"}]}]}
                self.assertFalse(collect._is_ai_workload(spec))

    def test_envfrom_alone_does_not_admit_a_workload(self):
        """A pod spec cannot say what is inside a Secret, so reading `envFrom`
        would admit anything that has one. On this fleet that is `ip-masq-agent`
        on five clusters and no model server anywhere."""
        spec = {"containers": [{
            "image": "gke.gcr.io/ip-masq-agent:v2.11",
            "envFrom": [{"configMapRef": {"name": "netd-config"}}],
        }]}
        self.assertFalse(collect._is_ai_workload(spec))


class TestNormalizeAiWorkloads(unittest.TestCase):
    def test_a_deployment_carries_its_pod_template_labels(self):
        d = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm", "tier": "serving"})
        out = collect.normalize_ai_workloads(dump_of(d))
        self.assertEqual(out[0]["lbl"], {"app": "vllm", "tier": "serving"})

    def test_a_cronjob_carries_the_nested_template_labels(self):
        c = ai_workload("CronJob", "batch-embed", pod_labels={"app": "batch-embed"})
        out = collect.normalize_ai_workloads(dump_of(c))
        self.assertEqual(out[0]["lbl"], {"app": "batch-embed"})

    def test_a_bare_pod_carries_its_own_labels(self):
        p = ai_workload("Pod", "one-off", pod_labels={"app": "one-off"})
        out = collect.normalize_ai_workloads(dump_of(p))
        self.assertEqual(out[0]["lbl"], {"app": "one-off"})

    def test_an_owned_pod_is_suppressed(self):
        p = ai_workload("Pod", "one-off")
        p["metadata"]["ownerReferences"] = [{"kind": "ReplicaSet", "name": "x"}]
        self.assertEqual(collect.normalize_ai_workloads(dump_of(p)), [])

    def test_a_system_namespace_is_suppressed(self):
        d = ai_workload("Deployment", "vllm", ns="kube-system")
        self.assertEqual(collect.normalize_ai_workloads(dump_of(d)), [])

    def test_a_non_ai_workload_never_appears(self):
        d = ai_workload("Deployment", "web", image="nginx:1.25")
        self.assertEqual(collect.normalize_ai_workloads(dump_of(d)), [])

    def test_an_addon_managed_object_is_suppressed_even_if_it_would_match(self):
        d = ai_workload("DaemonSet", "nvidia-gpu-device-plugin", container={"resources": {"limits": {"nvidia.com/gpu": "1"}}})
        d["metadata"]["labels"] = {"addonmanager.kubernetes.io/mode": "Reconcile"}
        self.assertEqual(collect.normalize_ai_workloads(dump_of(d)), [])

    def test_a_suspended_cronjob_is_kept_and_marked(self):
        c = ai_workload("CronJob", "batch-embed")
        c["spec"]["suspend"] = True
        out = collect.normalize_ai_workloads(dump_of(c))
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["suspended"])

    def test_a_running_workload_is_not_marked(self):
        for kind in ("Deployment", "CronJob", "Pod"):
            with self.subTest(kind=kind):
                self.assertFalse(collect.normalize_ai_workloads(dump_of(ai_workload(kind, "w")))[0]["suspended"])


class TestResolveArgv(unittest.TestCase):
    """Which tokens a flag parser in the container actually receives.

    The published defect this exists for: `CronJob/ai-batch-finetune` reported
    `--model … with no --revision` against `command: ["python3", "-c", "print(…)"]`,
    where the flag reaches a print statement and the prescribed `--revision` fix
    changes nothing.
    """

    def flags(self, container):
        return collect._resolve_argv(container).flags

    def tokens(self, container):
        return collect._resolve_argv(container).tokens

    def test_no_command_leaves_args_to_the_image_entrypoint(self):
        self.assertEqual(self.flags({"args": ["--model", "x"]}), ["--model", "x"])

    def test_a_plain_command_parses_the_whole_argv(self):
        self.assertEqual(
            self.flags({"command": ["python3", "-m", "vllm.entrypoints.api_server"], "args": ["--model", "x"]}),
            ["python3", "-m", "vllm.entrypoints.api_server", "--model", "x"],
        )

    def test_a_non_interpreter_command_is_never_read_as_inline_code(self):
        # `-c` means something else to most programs, so the inline-code rule
        # only applies to a shell or an interpreter that documents the flag.
        self.assertEqual(
            self.flags({"command": ["tritonserver", "-c", "cfg"], "args": ["--model", "x"]}),
            ["tritonserver", "-c", "cfg", "--model", "x"],
        )

    def test_an_interpreter_one_liner_parses_nothing(self):
        self.assertEqual(self.flags({"command": ["python3", "-c", "print('hi')"], "args": ["--model", "x"]}), [])

    def test_node_uses_the_other_inline_flag(self):
        self.assertEqual(self.flags({"command": ["node", "-e", "console.log(1)"], "args": ["--model", "x"]}), [])

    def test_an_interpreter_without_an_inline_flag_still_parses(self):
        self.assertEqual(
            self.flags({"command": ["python3", "serve.py"], "args": ["--model", "x"]}),
            ["python3", "serve.py", "--model", "x"],
        )

    def test_a_c_flag_after_the_script_path_belongs_to_the_script(self):
        # `python3 serve.py -c cfg.yaml --model x`: the interpreter's options end
        # at `serve.py`, so `-c` is the script's config flag, not inline code.
        self.assertEqual(
            self.flags({"command": ["python3", "serve.py", "-c", "cfg.yaml"], "args": ["--model", "x"]}),
            ["python3", "serve.py", "-c", "cfg.yaml", "--model", "x"],
        )

    def test_a_c_flag_after_a_module_belongs_to_the_module(self):
        self.assertEqual(
            self.flags({"command": ["python3", "-m", "serve", "-c", "cfg"], "args": ["--model", "x"]}),
            ["python3", "-m", "serve", "-c", "cfg", "--model", "x"],
        )

    def test_a_c_flag_after_a_shell_script_path_belongs_to_the_script(self):
        self.assertEqual(
            self.flags({"command": ["bash", "entry.sh", "-c", "cfg"], "args": ["--model", "x"]}),
            ["bash", "entry.sh", "-c", "cfg", "--model", "x"],
        )

    def test_a_shell_option_value_is_not_read_as_the_script_path(self):
        self.assertEqual(
            self.flags({"command": ["bash", "-o", "pipefail", "-c", "vllm --model foo"]}),
            ["vllm", "--model", "foo"],
        )

    def test_a_python_switch_is_not_read_as_taking_a_value(self):
        # `-O` and `-I` take no value under Python, so `-c` after them is still
        # the inline-code flag and nothing parses `--model`.
        for switch in ("-O", "-I"):
            self.assertEqual(
                self.flags({"command": ["python3", switch, "-c", "print(1)"], "args": ["--model", "meta/x"]}), []
            )

    def test_a_shells_m_is_the_monitor_switch_not_a_module(self):
        self.assertEqual(self.flags({"command": ["bash", "-m", "-c", "vllm serve --model foo"]}), ["vllm", "serve", "--model", "foo"])

    def test_a_shells_long_option_value_is_skipped(self):
        self.assertEqual(
            self.flags({"command": ["bash", "--rcfile", "rc", "-c", "vllm serve --model foo"]}),
            ["vllm", "serve", "--model", "foo"],
        )

    def test_bundled_and_long_interpreter_inline_flags_parse_nothing(self):
        for command in (
            ["python3", "-uc", "print(1)"],
            ["perl", "-le", "print 1"],
            ["ruby", "-we", "puts 1"],
            ["node", "--eval", "console.log(1)"],
            ["node", "-p", "1"],
            ["nodejs", "--print", "1"],
            ["python3", "--check-hash-based-pycs", "always", "-c", "print(1)"],
            ["node", "-r", "dotenv/config", "-e", "console.log(1)"],
            ["perl", "-I", "lib", "-e", "print 1"],
            ["ruby", "-C", "/app", "-e", "puts 1"],
            ["node", "--import", "tsx", "-e", "1"],
            ["node", "--experimental-loader", "ts-node/esm", "-e", "1"],
        ):
            with self.subTest(command=command):
                self.assertEqual(self.flags({"command": command, "args": ["--model", "meta/x"]}), [])

    def test_a_shell_one_liner_is_read_as_the_command_line_it_is(self):
        self.assertEqual(
            self.flags({"command": ["sh", "-c", "vllm serve --model foo"]}),
            ["vllm", "serve", "--model", "foo"],
        )

    def test_a_bundled_shell_flag_counts(self):
        self.assertEqual(self.flags({"command": ["/bin/bash", "-lc", "vllm --model foo"]}), ["vllm", "--model", "foo"])

    def test_the_shell_code_may_arrive_in_args(self):
        # `command: ["sh", "-c"]` with the script as `args[0]` is as common as
        # putting the whole thing in `command`.
        self.assertEqual(self.flags({"command": ["sh", "-c"], "args": ["vllm --model foo"]}), ["vllm", "--model", "foo"])

    def test_positionals_after_the_shell_code_are_not_flags(self):
        self.assertEqual(self.flags({"command": ["sh", "-c", "echo hi"], "args": ["--model", "x"]}), ["echo", "hi"])

    def test_unbalanced_quoting_reads_nothing_rather_than_guessing(self):
        self.assertEqual(self.flags({"command": ["sh", "-c", "vllm --model 'foo"]}), [])

    def test_an_empty_container_has_no_tokens(self):
        self.assertEqual(collect._resolve_argv({}), ([], []))

    def test_tokens_are_command_then_args(self):
        # Kubernetes order, and the order a value-after-flag read depends on.
        self.assertEqual(self.tokens({"command": ["serve", "--model"], "args": ["x"]}), ["serve", "--model", "x"])

    def test_shell_words_replace_the_code_string_they_came_from(self):
        # Replaced, not appended: keeping both reports one URL twice, the second
        # time with the whole command line labelled as the URL.
        self.assertEqual(
            self.tokens({"command": ["sh", "-c", "curl http://h/m -o /w"], "args": ["extra"]}),
            ["sh", "-c", "curl", "http://h/m", "-o", "/w", "extra"],
        )

    def test_an_interpreter_one_liner_keeps_its_source_in_the_tokens(self):
        # 3.2 reads `tokens`, and a directive written into the source is real.
        self.assertIn(
            "print('hi')", self.tokens({"command": ["python3", "-c", "print('hi')"], "args": ["--model", "x"]})
        )


class TestModelRemoteCodeTrusted(unittest.TestCase):
    def hit(self, container):
        w = ai_workload(container=container)
        w = collect.normalize_ai_workloads(dump_of(w))[0]
        return collect.check_model_remote_code_trusted(w, {})

    def test_flags_the_trust_remote_code_arg(self):
        self.assertIsNotNone(self.hit({"args": ["--trust-remote-code", "--model", "x"]}))

    def test_flags_the_underscore_spelling(self):
        self.assertIsNotNone(self.hit({"args": ["--trust_remote_code"]}))

    def test_does_not_flag_the_flag_explicitly_disabled(self):
        self.assertIsNone(self.hit({"args": ["--trust-remote-code=false"]}))

    def test_flags_a_truthy_env_var(self):
        self.assertIsNotNone(self.hit({"env": [{"name": "TRUST_REMOTE_CODE", "value": "true"}]}))

    def test_does_not_flag_a_falsy_env_var(self):
        self.assertIsNone(self.hit({"env": [{"name": "TRUST_REMOTE_CODE", "value": "false"}]}))

    def test_init_containers_count(self):
        w = ai_workload()
        w["spec"]["template"]["spec"]["initContainers"] = [{"name": "fetch", "args": ["--trust-remote-code"]}]
        w = collect.normalize_ai_workloads(dump_of(w))[0]
        hit = collect.check_model_remote_code_trusted(w, {})
        self.assertIn("fetch", hit["excerpt"])

    def test_the_excerpt_names_which_setting_fired(self):
        """The 2026-09-06 08:55Z report published `containers: embeddings` as
        the evidence for a recommendation to remove an environment variable.
        The two arms take different edits, so the container name alone does not
        let a reader check the fix against what was found."""
        env_hit = self.hit({"env": [{"name": "TRUST_REMOTE_CODE", "value": "true"}]})
        self.assertIn("env TRUST_REMOTE_CODE=true", env_hit["excerpt"])
        arg_hit = self.hit({"args": ["--trust-remote-code", "--model", "x"]})
        self.assertIn("arg setting trust-remote-code", arg_hit["excerpt"])
        self.assertNotIn("env ", arg_hit["excerpt"])

    def test_each_flagged_container_carries_its_own_reason(self):
        w = ai_workload(container={"args": ["--trust-remote-code"]})
        w["spec"]["template"]["spec"]["containers"][0]["name"] = "server"
        w["spec"]["template"]["spec"]["initContainers"] = [
            {"name": "fetch", "env": [{"name": "TRUST_REMOTE_CODE", "value": "1"}]}
        ]
        w = collect.normalize_ai_workloads(dump_of(w))[0]
        excerpt = collect.check_model_remote_code_trusted(w, {})["excerpt"]
        self.assertIn("server (arg setting trust-remote-code)", excerpt)
        self.assertIn("fetch (env TRUST_REMOTE_CODE=1)", excerpt)

    def test_a_directive_inside_an_interpreter_one_liner_still_counts(self):
        # 3.4's flag arm reads nothing here, because nothing parses flags. 3.2
        # reads everything, because `trust_remote_code=True` written into the
        # source *is* the instruction to the loader, not an inert argument.
        self.assertIsNotNone(
            self.hit(
                {
                    "command": [
                        "python3",
                        "-c",
                        "from transformers import AutoModel; AutoModel.from_pretrained('m', trust_remote_code=True)",
                    ]
                }
            )
        )

    def test_a_directive_inside_a_shell_one_liner_counts(self):
        self.assertIsNotNone(self.hit({"command": ["sh", "-c", "vllm serve --trust-remote-code"]}))


class TestWeightsMountWritable(unittest.TestCase):
    def collected(self, container, volumes):
        w = ai_workload(container=container, volumes=volumes)
        return collect.normalize_ai_workloads(dump_of(w))[0]

    def test_flags_a_readwrite_csi_mount(self):
        w = self.collected(
            {"volumeMounts": [{"name": "weights", "mountPath": "/weights"}]},
            [{"name": "weights", "csi": {"driver": "gcsfuse.csi.storage.gke.io"}}],
        )
        self.assertIsNotNone(collect.check_weights_mount_writable(w, {}))

    def test_flags_a_readwrite_pvc_mount(self):
        w = self.collected(
            {"volumeMounts": [{"name": "weights", "mountPath": "/weights"}]},
            [{"name": "weights", "persistentVolumeClaim": {"claimName": "weights-pvc"}}],
        )
        self.assertIsNotNone(collect.check_weights_mount_writable(w, {}))

    def test_does_not_flag_a_readonly_mount(self):
        w = self.collected(
            {"volumeMounts": [{"name": "weights", "mountPath": "/weights", "readOnly": True}]},
            [{"name": "weights", "csi": {"driver": "x"}}],
        )
        self.assertIsNone(collect.check_weights_mount_writable(w, {}))

    def test_does_not_flag_a_readonly_volume(self):
        w = self.collected(
            {"volumeMounts": [{"name": "weights", "mountPath": "/weights"}]},
            [{"name": "weights", "csi": {"driver": "x", "readOnly": True}}],
        )
        self.assertIsNone(collect.check_weights_mount_writable(w, {}))

    def test_does_not_flag_an_emptydir(self):
        w = self.collected(
            {"volumeMounts": [{"name": "scratch", "mountPath": "/tmp"}]},
            [{"name": "scratch", "emptyDir": {}}],
        )
        self.assertIsNone(collect.check_weights_mount_writable(w, {}))

    def test_the_join_by_name_is_required_not_a_direct_field_test(self):
        # Regression for the exact bug the SOP calls load-bearing: csi/pvc
        # live on the volume, never on the mount, so a mount naming a
        # volume that does not exist must not spuriously match.
        w = self.collected({"volumeMounts": [{"name": "missing", "mountPath": "/x"}]}, [])
        self.assertIsNone(collect.check_weights_mount_writable(w, {}))

    def test_names_env_paths_that_the_flagged_container_writes_into(self):
        # An auto-merged remediation added readOnly: true to a mount whose own
        # container had HOME set inside it, and the pod went to CrashLoopBackOff
        # with "remove /models/.ollama/models/manifests: read-only file system".
        # The evidence has to carry the conflict or the fix cannot see it.
        w = self.collected(
            {
                "volumeMounts": [{"name": "weights", "mountPath": "/models"}],
                "env": [
                    {"name": "HOME", "value": "/models"},
                    {"name": "HF_HOME", "value": "/models/.cache"},
                ],
            },
            [{"name": "weights", "persistentVolumeClaim": {"claimName": "w"}}],
        )
        hit = collect.check_weights_mount_writable(w, {})
        self.assertIn("HOME=/models", hit["excerpt"])
        self.assertIn("HF_HOME=/models/.cache", hit["excerpt"])

    def test_env_outside_the_mount_is_not_reported_as_a_writer(self):
        # /models-cache is a sibling of /models, not a path inside it; a prefix
        # test without the separator would call it a conflict and push the
        # remediation into a manual finding for no reason.
        w = self.collected(
            {
                "volumeMounts": [{"name": "weights", "mountPath": "/models"}],
                "env": [
                    {"name": "HOME", "value": "/state"},
                    {"name": "CACHE", "value": "/models-cache"},
                ],
            },
            [{"name": "weights", "persistentVolumeClaim": {"claimName": "w"}}],
        )
        hit = collect.check_weights_mount_writable(w, {})
        self.assertNotIn("container writes here", hit["excerpt"])


class TestModelArtifactUnpinnedSource(unittest.TestCase):
    def hit(self, container):
        w = ai_workload(container=container)
        w = collect.normalize_ai_workloads(dump_of(w))[0]
        return collect.check_model_artifact_unpinned_source(w, {})

    def test_flags_a_plaintext_http_url(self):
        self.assertIsNotNone(self.hit({"args": ["--weights", "http://example.com/model.bin"]}))

    def test_flags_an_ftp_url(self):
        self.assertIsNotNone(self.hit({"env": [{"name": "MODEL_URL", "value": "ftp://example.com/model.bin"}]}))

    def test_does_not_flag_an_https_url(self):
        self.assertIsNone(self.hit({"args": ["--weights", "https://example.com/model.bin"]}))

    def test_does_not_flag_an_object_store_uri(self):
        self.assertIsNone(self.hit({"args": ["--weights", "gs://bucket/model.bin"]}))

    def test_flags_model_without_revision(self):
        self.assertIsNotNone(self.hit({"args": ["--model", "meta-llama/Llama-3"]}))

    def test_does_not_flag_model_with_revision(self):
        self.assertIsNone(self.hit({"args": ["--model", "meta-llama/Llama-3", "--revision", "abc123"]}))

    def test_accepts_the_equals_spelling_of_both_flags(self):
        self.assertIsNone(self.hit({"args": ["--model=meta-llama/Llama-3", "--revision=abc123"]}))
        self.assertIsNotNone(self.hit({"args": ["--model=meta-llama/Llama-3"]}))

    def test_escalates_to_critical_alongside_a_remote_code_finding_on_the_same_container(self):
        hit = self.hit({"args": ["--model", "x", "--trust-remote-code"]})
        self.assertEqual(hit["severity"], "critical")

    def test_does_not_escalate_without_a_remote_code_finding(self):
        hit = self.hit({"args": ["--model", "x"]})
        self.assertNotIn("severity", hit)

    def test_excerpt_names_the_offending_url(self):
        hit = self.hit({"args": ["--weights", "http://example.com/model.bin"]})
        self.assertIn("http://example.com/model.bin", hit["excerpt"])

    def test_excerpt_names_the_model_a_bare_flag_leaves_in_the_next_argument(self):
        hit = self.hit({"args": ["--model", "meta-llama/Llama-3"]})
        self.assertIn("--model meta-llama/Llama-3", hit["excerpt"])
        self.assertIn("no --revision", hit["excerpt"])

    def test_excerpt_reports_both_conditions_on_one_container(self):
        hit = self.hit({"args": ["--model", "m", "--weights", "http://example.com/w.bin"]})
        self.assertIn("plaintext URL", hit["excerpt"])
        self.assertIn("no --revision", hit["excerpt"])

    def test_excerpt_strips_url_userinfo_and_query_string(self):
        # The ledger is a public GitHub issue; a signed-URL token or a
        # basic-auth password in the manifest must not be republished there.
        hit = self.hit({"args": ["--weights", "http://user:pw@example.com/m.bin?sig=SECRET"]})
        self.assertNotIn("SECRET", hit["excerpt"])
        self.assertNotIn("pw", hit["excerpt"])
        self.assertIn("example.com/m.bin", hit["excerpt"])

    def test_does_not_flag_a_model_flag_no_entrypoint_parses(self):
        # The published defect: `python3 -c "print(…)"` puts `--model` in
        # `sys.argv` beside a print statement. No model is fetched, so there is
        # no unpinned source, and adding `--revision` beside it fixes nothing.
        self.assertIsNone(
            self.hit({"command": ["python3", "-c", "print('never scheduled')"], "args": ["--model", "m"]})
        )

    def test_still_flags_a_model_flag_a_real_entrypoint_parses(self):
        self.assertIsNotNone(
            self.hit({"command": ["python3", "-m", "vllm.entrypoints.api_server"], "args": ["--model", "m"]})
        )

    def test_flags_a_model_reference_inside_a_shell_one_liner(self):
        # The flag is mid-token here, so the token-prefix read that predates
        # `_container_flag_argv` found nothing at all.
        hit = self.hit({"command": ["sh", "-c", "vllm serve --model meta-llama/Llama-3"]})
        self.assertIn("--model meta-llama/Llama-3", hit["excerpt"])

    def test_a_revision_inside_the_same_shell_one_liner_clears_it(self):
        self.assertIsNone(self.hit({"command": ["sh", "-c", "vllm serve --model m --revision abc123"]}))

    def test_a_url_is_still_reported_when_no_entrypoint_parses_flags(self):
        # The flag arm goes quiet; the fetch does not. Whoever issues it, a
        # plaintext download is a plaintext download.
        hit = self.hit(
            {
                "command": ["python3", "-c", "print('x')"],
                "env": [{"name": "MODEL_URL", "value": "http://example.com/m.bin"}],
            }
        )
        self.assertIn("http://example.com/m.bin", hit["excerpt"])
        self.assertNotIn("no --revision", hit["excerpt"])

    def test_a_url_in_the_argv_survives_an_entrypoint_that_parses_no_flags(self):
        # Same point as the test above, one step further in: the URL arm reads
        # every token, not the narrowed flag set, so it finds this one even
        # though nothing in the container parses `--weights`.
        hit = self.hit({"command": ["python3", "-c", "print('x')"], "args": ["--weights", "http://example.com/m.bin"]})
        self.assertIn("http://example.com/m.bin", hit["excerpt"])

    def test_a_url_in_both_an_argument_and_an_env_var_is_reported_once(self):
        hit = self.hit(
            {
                "args": ["--weights", "http://example.com/m.bin"],
                "env": [{"name": "MODEL_URL", "value": "http://example.com/m.bin"}],
            }
        )
        self.assertEqual(hit["excerpt"].count("example.com/m.bin"), 1)

    def test_finds_a_url_buried_mid_token_in_a_shell_one_liner(self):
        hit = self.hit({"command": ["sh", "-c", "curl http://example.com/m.bin -o /w"]})
        self.assertIn("http://example.com/m.bin", hit["excerpt"])

    def test_reports_a_url_once_when_it_is_both_a_token_and_a_shell_word(self):
        hit = self.hit({"command": ["sh", "-c", "http://example.com/m.bin | tee /w"]})
        self.assertEqual(hit["excerpt"].count("example.com/m.bin"), 1)

    def test_pairs_a_flag_ending_command_with_its_value_in_args(self):
        # `command + args` is the kubelet's order; the reversed concatenation
        # this replaced reported `--model` with nothing beside it.
        hit = self.hit({"command": ["serve", "--model"], "args": ["meta-llama/Llama-3"]})
        self.assertIn("--model meta-llama/Llama-3", hit["excerpt"])


class TestModelCredentialPlaintextEnv(unittest.TestCase):
    def hit(self, env):
        w = ai_workload(container={"env": env})
        w = collect.normalize_ai_workloads(dump_of(w))[0]
        return collect.check_model_credential_plaintext_env(w, {})

    def test_flags_a_literal_hf_token(self):
        self.assertIsNotNone(self.hit([{"name": "HF_TOKEN", "value": "hf_xxx"}]))

    def test_does_not_flag_a_secretkeyref(self):
        self.assertIsNone(self.hit([{"name": "HF_TOKEN", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}}]))

    def test_does_not_flag_an_empty_value(self):
        self.assertIsNone(self.hit([{"name": "HF_TOKEN", "value": ""}]))

    def test_flags_openai_and_anthropic_keys(self):
        self.assertIsNotNone(self.hit([{"name": "OPENAI_API_KEY", "value": "sk-x"}]))
        self.assertIsNotNone(self.hit([{"name": "ANTHROPIC_API_KEY", "value": "sk-ant-x"}]))

    def test_never_puts_the_value_in_the_excerpt(self):
        hit = self.hit([{"name": "HF_TOKEN", "value": "hf_super_secret_value"}])
        self.assertNotIn("hf_super_secret_value", hit["excerpt"])

    def test_flags_a_provider_only_the_scope_prong_names(self):
        """`COHERE_API_KEY` matches no clause of this check's own name rule --
        no `MODEL`/`REGISTRY`/`INFERENCE` in it, and not one of the four spelled
        out. Without the union it would admit a workload to the audit through
        `_is_ai_workload` and then be the one literal the audit does not read,
        which is the worst of both."""
        for env_name in ("COHERE_API_KEY", "MISTRAL_API_KEY", "GEMINI_API_KEY", "REPLICATE_API_TOKEN"):
            with self.subTest(env_name=env_name):
                self.assertIsNotNone(self.hit([{"name": env_name, "value": "sk-live-abcdef123456"}]))

    def test_the_union_does_not_defeat_the_safe_suffix_rule(self):
        """`_FILE` still names a path rather than a value, whichever of the two
        name rules matched upstream of the suffix."""
        self.assertIsNone(self.hit([{"name": "COHERE_API_KEY_FILE", "value": "/var/run/secrets/cohere"}]))

    def test_does_not_flag_the_sops_own_named_non_secret_examples(self):
        self.assertIsNone(self.hit([{"name": "HF_TOKEN_PATH", "value": "/var/run/secrets/hf/token"}]))
        self.assertIsNone(self.hit([{"name": "OPENAI_API_KEY_FILE", "value": "/etc/openai/key"}]))
        self.assertIsNone(self.hit([{"name": "MODEL_REGISTRY_KEY_ID", "value": "key-2026-01"}]))

    def test_a_live_looking_token_keeps_the_default_severity(self):
        self.assertIsNone(self.hit([{"name": "HF_TOKEN", "value": "hf_qMBpTvKzLdWnXaHrYuEjCiSoPfGb"}]).get("severity"))

    def test_a_placeholder_is_reported_but_downgraded(self):
        # The exact value the ai-inference demo ships in this fleet. Reported
        # so nobody has to trust the heuristic, minor so it does not read as
        # a leaked credential.
        hit = self.hit([{"name": "HF_TOKEN", "value": "hf_EXAMPLE_PLACEHOLDER_NOT_A_REAL_TOKEN"}])
        self.assertEqual(hit["severity"], "minor")
        self.assertIn("placeholder", hit["excerpt"])
        self.assertIn("server:HF_TOKEN", hit["excerpt"])

    def test_an_unexpanded_reference_is_downgraded(self):
        for value in ("$(HF_TOKEN_REF)", "${HF_TOKEN}", "{{ .Values.hfToken }}"):
            self.assertEqual(self.hit([{"name": "HF_TOKEN", "value": value}])["severity"], "minor")

    def test_the_downgraded_arm_states_no_credential_is_embedded(self):
        """The `major` sentence is false on this arm, and §3.5 forbids it.

        The arm fired because every value reads as a placeholder, so the spec
        constant -- "a model-registry credential is embedded ... not rotatable
        without a redeploy" -- contradicts the reason the finding is `minor`
        in the same breath, and sends the owner to rotate a credential that
        does not exist. Shipped that way on 2026-09-05.
        """
        hit = self.hit([{"name": "HF_TOKEN", "value": "hf_EXAMPLE_PLACEHOLDER_NOT_A_REAL_TOKEN"}])
        spec_impact = next(s for s in collect.AI_SECURITY_CHECKS if s.slug == "model-credential-plaintext-env").impact
        self.assertNotEqual(hit["impact"], spec_impact)
        self.assertNotIn("is embedded", hit["impact"])
        self.assertIn("placeholder", hit["impact"])

    def test_a_live_looking_token_keeps_the_default_impact(self):
        # The `major` arm must stay unflagged so the model's object-specific
        # rewrite of the constant survives, exactly as `no-pdb` does.
        self.assertIsNone(self.hit([{"name": "HF_TOKEN", "value": "hf_qMBpTvKzLdWnXaHrYuEjCiSoPfGb"}]).get("impact"))

    def test_the_downgraded_arms_impact_never_carries_the_value(self):
        hit = self.hit([{"name": "HF_TOKEN", "value": "hf_EXAMPLE_PLACEHOLDER_NOT_A_REAL_TOKEN"}])
        self.assertNotIn("NOT_A_REAL_TOKEN", hit["impact"])

    def test_a_placeholder_beside_a_live_token_stays_major(self):
        # One real credential is not made safe by the placeholders next to it,
        # so the downgrade requires every value to be inert.
        hit = self.hit(
            [
                {"name": "HF_TOKEN", "value": "hf_EXAMPLE_PLACEHOLDER"},
                {"name": "OPENAI_API_KEY", "value": "sk-qMBpTvKzLdWnXaHrYuEj"},
            ]
        )
        self.assertIsNone(hit.get("severity"))

    def test_the_placeholder_value_still_never_reaches_the_excerpt(self):
        hit = self.hit([{"name": "HF_TOKEN", "value": "hf_EXAMPLE_PLACEHOLDER_NOT_A_REAL_TOKEN"}])
        self.assertNotIn("NOT_A_REAL_TOKEN", hit["excerpt"])

    def test_a_live_token_that_merely_contains_a_placeholder_word_stays_major(self):
        # A substring test downgrades every one of these: "todo" inside a
        # random run, "sample" inside another, and -- worst -- a DSN whose
        # *hostname* is example.com while its password is live. Suppressing a
        # real credential is the expensive error, so the whole value has to
        # read as inert, not some part of it.
        for value in (
            "sk-proj-Todo7xKqPnVrLmZbHdGw",
            "hf_QsampleWnXaHrYuEjCiSoPfGb",
            "postgres://svc:9fKq2LmZbHdGw@db.example.com:5432/models",
            "AKIAIOSFODNN7EXAMPLE",
        ):
            with self.subTest(value=value):
                self.assertIsNone(self.hit([{"name": "HF_TOKEN", "value": value}]).get("severity"))

    def test_a_live_token_carrying_reference_punctuation_stays_major(self):
        # `${`, `$(` and `{{` anywhere in the value used to inert it, so any
        # secret whose alphabet includes them was silently downgraded.
        for value in ("sk-live-qMBpTvKzLdWnX${", "hf_2LmZbHdGw{{PnVrKq", "pw:$(9fKq2LmZbHdGwXa"):
            with self.subTest(value=value):
                self.assertIsNone(self.hit([{"name": "HF_TOKEN", "value": value}]).get("severity"))

    def test_a_placeholder_written_without_separators_is_left_at_major(self):
        # Deliberate, and the direction to err in. Splitting on non-alphanumerics
        # is what makes `hf_EXAMPLE_PLACEHOLDER_NOT_A_REAL_TOKEN` readable, and
        # the cost is that a run-together placeholder has no separators to split
        # on, so `CHANGEME` reads as one opaque token and keeps `major`.
        #
        # The obvious repair -- letting a token match a *sequence* of placeholder
        # words -- is what must not be done: `SECRET`+`KEY` tiles `secretkey`,
        # which is a weak password rather than a placeholder, and downgrading a
        # live credential is the error this check cannot afford. Over-reporting
        # a placeholder is the one it can. `ai_security_audit_sop.md` says the
        # same under check 3.5: never dropped, only downgraded.
        for value in ("CHANGEME", "YOURTOKENHERE"):
            with self.subTest(value=value):
                self.assertIsNone(self.hit([{"name": "HF_TOKEN", "value": value}]).get("severity"))

    def test_the_excerpt_reads_correctly_beside_a_severity_it_did_not_set(self):
        # `adopt_collector_evidence` forces this excerpt onto the finding but
        # never copies `severity`, so a model that kept `major` gets this
        # sentence under it. It has to describe what was measured rather than
        # assert the conclusion, or the report contradicts itself.
        hit = self.hit([{"name": "HF_TOKEN", "value": "hf_EXAMPLE_PLACEHOLDER_NOT_A_REAL_TOKEN"}])
        self.assertNotIn("not a live secret", hit["excerpt"])
        self.assertIn("matches this check's placeholder or unexpanded-reference patterns", hit["excerpt"])


PLACEHOLDER = "hf_EXAMPLE_PLACEHOLDER_NOT_A_REAL_TOKEN"


def secret_ref(name="ai-inference-model-credentials", key="hf-token", optional=True, var="HF_TOKEN"):
    ref = {"name": name, "key": key}
    if optional is not None:
        ref["optional"] = optional
    return {"name": var, "valueFrom": {"secretKeyRef": ref}}


class TestNamespaceSecretRefReplacement(unittest.TestCase):
    """The one path on which §3.5's `kind: manual, always` gives way.

    The replacement has to be read off a live object, never invented: this
    audit cannot read Secrets at all, so an `optional: true` reference some
    container in the same namespace is already running is the only evidence it
    can have that the swap is safe. `adopt_collector_evidence` overwrites the
    model's excerpt with this one, so the coordinates have to travel in the
    excerpt or they never reach the finding.
    """

    def hit(self, env, *, siblings=(), ns="ai-inference"):
        w = ai_workload(name="ai-inference-unsafe", ns=ns, container={"env": env})
        workloads = collect.normalize_ai_workloads(dump_of(w, *siblings))
        mine = next(x for x in workloads if x["name"] == "ai-inference-unsafe")
        return collect.check_model_credential_plaintext_env(mine, {"ai_workloads": workloads})

    def sibling(self, env, *, name="ai-inference-hardened", ns="ai-inference"):
        return ai_workload(name=name, ns=ns, container={"env": env})

    def test_a_sibling_reference_is_named_in_the_excerpt(self):
        hit = self.hit(
            [{"name": "HF_TOKEN", "value": PLACEHOLDER}],
            siblings=[self.sibling([secret_ref()])],
        )
        self.assertEqual(hit["severity"], "minor")
        self.assertIn("HF_TOKEN -> secretKeyRef name=ai-inference-model-credentials key=hf-token", hit["excerpt"])
        self.assertIn("optional=true", hit["excerpt"])
        self.assertNotIn(PLACEHOLDER, hit["excerpt"])

    def test_no_sibling_leaves_the_finding_manual(self):
        hit = self.hit([{"name": "HF_TOKEN", "value": PLACEHOLDER}])
        self.assertEqual(hit["severity"], "minor")
        self.assertNotIn("secretKeyRef", hit["excerpt"])

    def test_a_live_looking_value_is_never_offered_a_replacement(self):
        """The `major` arm, where the swap could discard a real credential.

        The reference points at a Secret whose contents this audit cannot read,
        so pointing a workload holding a live token at it either changes which
        credential is used or drops it. Only the arm that has already
        established every value is a placeholder has nothing to lose.
        """
        hit = self.hit(
            [{"name": "HF_TOKEN", "value": "hf_qMBpTvKzLdWnXaHrYuEjCiSoPfGb"}],
            siblings=[self.sibling([secret_ref()])],
        )
        self.assertIsNone(hit.get("severity"))
        self.assertNotIn("secretKeyRef", hit["excerpt"])

    def test_a_non_optional_reference_is_not_copied(self):
        """Without `optional: true` a Secret that does not exist is
        `CreateContainerConfigError`, and this audit cannot check whether it
        exists -- so it would be trading a `minor` finding about a placeholder
        for a stopped workload."""
        for optional in (False, None):
            with self.subTest(optional=optional):
                hit = self.hit(
                    [{"name": "HF_TOKEN", "value": PLACEHOLDER}],
                    siblings=[self.sibling([secret_ref(optional=optional)])],
                )
                self.assertNotIn("secretKeyRef", hit["excerpt"])

    def test_a_reference_in_another_namespace_is_not_copied(self):
        """A Secret is namespaced, so the reference would not resolve."""
        hit = self.hit(
            [{"name": "HF_TOKEN", "value": PLACEHOLDER}],
            siblings=[self.sibling([secret_ref()], ns="other")],
        )
        self.assertNotIn("secretKeyRef", hit["excerpt"])

    def test_two_siblings_disagreeing_name_nothing(self):
        """Choosing between them would be inventing the Secret name §4 forbids."""
        hit = self.hit(
            [{"name": "HF_TOKEN", "value": PLACEHOLDER}],
            siblings=[
                self.sibling([secret_ref(name="creds-a")], name="a"),
                self.sibling([secret_ref(name="creds-b")], name="b"),
            ],
        )
        self.assertNotIn("secretKeyRef", hit["excerpt"])

    def test_two_siblings_agreeing_still_name_the_reference(self):
        hit = self.hit(
            [{"name": "HF_TOKEN", "value": PLACEHOLDER}],
            siblings=[self.sibling([secret_ref()], name="a"), self.sibling([secret_ref()], name="b")],
        )
        self.assertIn("secretKeyRef name=ai-inference-model-credentials", hit["excerpt"])

    def test_one_variable_short_offers_nothing(self):
        """All or nothing: a rewrite covering `HF_TOKEN` and not `OPENAI_API_KEY`
        leaves this same finding standing on the variable it missed."""
        hit = self.hit(
            [
                {"name": "HF_TOKEN", "value": PLACEHOLDER},
                {"name": "OPENAI_API_KEY", "value": "${OPENAI_KEY}"},
            ],
            siblings=[self.sibling([secret_ref()])],
        )
        self.assertNotIn("secretKeyRef", hit["excerpt"])

    def test_every_variable_covered_names_them_all(self):
        hit = self.hit(
            [
                {"name": "HF_TOKEN", "value": PLACEHOLDER},
                {"name": "OPENAI_API_KEY", "value": "${OPENAI_KEY}"},
            ],
            siblings=[
                self.sibling([secret_ref(), secret_ref(var="OPENAI_API_KEY", name="openai", key="api-key")])
            ],
        )
        self.assertIn("HF_TOKEN -> secretKeyRef name=ai-inference-model-credentials key=hf-token", hit["excerpt"])
        self.assertIn("OPENAI_API_KEY -> secretKeyRef name=openai key=api-key", hit["excerpt"])


class TestModelImageFloatingTag(unittest.TestCase):
    def hit(self, image):
        # The discriminator is independent of the image under test here, so
        # pin it via the accelerator prong -- otherwise an image string that
        # does not happen to name a known model server (e.g. a bare registry
        # host) would drop the workload out of scope before this check ever
        # saw it.
        w = ai_workload(container={"image": image, "resources": {"limits": {"nvidia.com/gpu": "1"}}})
        w = collect.normalize_ai_workloads(dump_of(w))[0]
        return collect.check_model_image_floating_tag(w, {})

    def test_flags_latest(self):
        self.assertIsNotNone(self.hit("acme/vllm:latest"))

    def test_flags_no_tag_at_all(self):
        self.assertIsNotNone(self.hit("acme/vllm"))

    def test_does_not_flag_a_version_tag(self):
        self.assertIsNone(self.hit("acme/vllm:v0.6.2"))

    def test_does_not_flag_a_digest_even_with_a_floating_tag(self):
        self.assertIsNone(self.hit("acme/vllm:latest@sha256:" + "a" * 64))

    def test_a_registry_port_is_not_mistaken_for_a_tag(self):
        # gcr.io:5000/i has a colon with a `/` after it before the string
        # ends -- that is not a tag, so this is the untagged case, not a
        # false negative.
        self.assertIsNotNone(self.hit("gcr.io:5000/i"))
        self.assertIsNone(self.hit("gcr.io:5000/i:v1"))


class TestInferenceEndpointPublic(unittest.TestCase):
    def result(self, svc, workloads):
        context = {"services": [svc], "ai_workloads": collect.normalize_ai_workloads(dump_of(*workloads))}
        return collect.check_inference_endpoint_public(context)

    def test_flags_a_public_loadbalancer_selecting_an_ai_workload(self):
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"})
        svc = ai_service("vllm-svc", selector={"app": "vllm"})
        hits = self.result(svc, [w])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "Service/vllm-svc")

    def test_does_not_flag_clusterip(self):
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"})
        svc = ai_service("vllm-svc", selector={"app": "vllm"}, svc_type="ClusterIP")
        self.assertEqual(self.result(svc, [w]), [])

    def test_does_not_flag_the_current_internal_lb_annotation(self):
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"})
        svc = ai_service("vllm-svc", selector={"app": "vllm"}, annotations={"networking.gke.io/load-balancer-type": "Internal"})
        self.assertEqual(self.result(svc, [w]), [])

    def test_does_not_flag_the_legacy_internal_lb_annotation(self):
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"})
        svc = ai_service("vllm-svc", selector={"app": "vllm"}, annotations={"cloud.google.com/load-balancer-type": "Internal"})
        self.assertEqual(self.result(svc, [w]), [])

    def test_does_not_flag_a_selector_less_service(self):
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"})
        svc = ai_service("headless", selector={})
        self.assertEqual(self.result(svc, [w]), [])

    def test_does_not_flag_a_loadbalancer_selecting_a_non_ai_workload(self):
        svc = ai_service("web-svc", selector={"app": "web"})
        self.assertEqual(self.result(svc, []), [])

    def test_the_selector_must_be_a_subset_not_an_exact_match(self):
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm", "tier": "serving"})
        svc = ai_service("vllm-svc", selector={"app": "vllm"})
        self.assertEqual(len(self.result(svc, [w])), 1)

    def test_an_rfc1918_address_is_not_a_public_endpoint(self):
        # The annotation says what was asked for; the assigned address says
        # what was given. A private address means unreachable, so the finding
        # would be untrue however the annotations read.
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"})
        svc = ai_service("vllm-svc", selector={"app": "vllm"}, ingress=["10.150.0.78"])
        self.assertEqual(self.result(svc, [w]), [])

    def test_a_routable_address_is_flagged_without_publishing_the_address(self):
        # `ai_security_audit_sop.md` Red Lines: the address of a reachable
        # model endpoint never reaches `title`, `object`, `evidence.excerpt`
        # or `recommendation`. These findings are filed as issues on a public
        # repository, and `adopt_collector_evidence` forces this excerpt over
        # whatever the model wrote, so the rule has to hold here or nowhere.
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"})
        svc = ai_service("vllm-svc", selector={"app": "vllm"}, ingress=["136.70.153.197"])
        hits = self.result(svc, [w])
        self.assertEqual(len(hits), 1)
        self.assertNotIn("136.70.153.197", hits[0]["excerpt"])
        self.assertNotIn("136.70.153.197", hits[0]["object"])
        self.assertIn("1 assigned address, none of them private", hits[0]["excerpt"])

    def test_one_public_address_among_private_ones_still_counts(self):
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"})
        svc = ai_service("vllm-svc", selector={"app": "vllm"}, ingress=["10.0.0.5", "136.70.153.197"])
        hits = self.result(svc, [w])
        self.assertEqual(len(hits), 1)
        self.assertNotIn("136.70.153.197", hits[0]["excerpt"])
        self.assertNotIn("10.0.0.5", hits[0]["excerpt"])
        self.assertIn("2 assigned addresses, 1 of them not private", hits[0]["excerpt"])

    def test_an_ingress_entry_carrying_both_ip_and_hostname_keeps_the_hostname(self):
        # `ip or hostname` short-circuits and would throw the hostname away.
        # The private IP would then be the only address considered, the
        # all-private branch would fire, and a reachable endpoint would go
        # unreported -- the one direction that loses a finding.
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"})
        svc = ai_service("vllm-svc", selector={"app": "vllm"})
        svc["status"] = {"loadBalancer": {"ingress": [{"ip": "10.0.0.5", "hostname": "vllm.example.com"}]}}
        hits = self.result(svc, [w])
        self.assertEqual(len(hits), 1)
        self.assertNotIn("vllm.example.com", hits[0]["excerpt"])

    def test_a_pending_load_balancer_still_falls_back_to_annotations(self):
        # No address assigned yet, so status says nothing and the annotation
        # is all there is -- the behaviour before this check read status.
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"})
        self.assertEqual(len(self.result(ai_service("vllm-svc", selector={"app": "vllm"}, ingress=[]), [w])), 1)

    def test_a_hostname_is_treated_as_public_rather_than_dropped(self):
        # The collector resolves nothing, so an unresolvable address keeps the
        # finding instead of silently clearing it.
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"})
        svc = ai_service("vllm-svc", selector={"app": "vllm"})
        svc["status"] = {"loadBalancer": {"ingress": [{"hostname": "a1b2.elb.amazonaws.com"}]}}
        self.assertEqual(len(self.result(svc, [w])), 1)

    def restricted(self, ranges, ingress=("136.70.153.197",)):
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"})
        svc = ai_service("vllm-svc", selector={"app": "vllm"}, ingress=list(ingress))
        svc["spec"]["loadBalancerSourceRanges"] = ranges
        hits = self.result(svc, [w])
        self.assertEqual(len(hits), 1)
        return hits[0]

    def test_a_source_range_allowlist_downgrades_rather_than_drops(self):
        # The check's `impact` says anyone who finds the address can use the
        # endpoint. GKE programs `loadBalancerSourceRanges` into the firewall
        # in front of the forwarding rule, so under an allowlist that is not
        # true and `critical` overstates it. Still a finding, though -- an
        # allowlist bounds who reaches the endpoint, it does not make it
        # unreachable, so this downgrades where the private-address branch
        # drops outright.
        hit = self.restricted(["35.191.0.0/16"])
        self.assertEqual(hit["severity"], "major")
        self.assertIn("admits 1 CIDR, not the whole internet", hit["excerpt"])

    def test_the_downgraded_arm_carries_its_own_impact(self):
        # `emit` derives `impact_authoritative` from the hit's own `impact`, so
        # an arm that moves the severity and leaves the impact ships the
        # unrestricted `critical` sentence on a `major` finding and gives
        # `adopt_arm_impact` nothing to hold the model to.
        hit = self.restricted(["35.191.0.0/16"])
        self.assertIn("bounded by the CIDR allowlist", hit["impact"])
        self.assertNotIn("anyone who finds the address", hit["impact"].lower())

    def test_the_allowed_ranges_are_counted_never_printed(self):
        # Which networks are trusted is the other half of the target, and
        # these findings are filed as issues on a public repository.
        hit = self.restricted(["35.191.0.0/16", "130.211.0.7/32"])
        self.assertNotIn("35.191", hit["excerpt"])
        self.assertNotIn("130.211", hit["excerpt"])
        self.assertIn("admits 2 CIDRs", hit["excerpt"])

    def test_an_allowlist_of_only_documentation_prefixes_admits_nobody(self):
        """`192.0.2.0/24` is RFC 5737 TEST-NET-1: no host anywhere is assigned
        one, so the allowlist lets nothing through. The 2026-09-05 report
        published `major` on exactly this Service and told the operator that
        "anyone connecting from within that allowed range can send it inference
        traffic, consume its accelerator capacity, and probe whatever the model
        can reach"."""
        hit = self.restricted(["192.0.2.0/24"])
        self.assertEqual(hit["severity"], "minor")
        self.assertIn("reserved documentation prefix", hit["excerpt"])
        self.assertIn("admits no caller at all", hit["excerpt"])
        self.assertIn("no caller can reach", hit["impact"])
        # Still published: a public address and forwarding rule serving nobody
        # is a misconfiguration, even though it is not an exposure.
        self.assertIn("admits 1 CIDR", hit["excerpt"])

    def test_every_documentation_block_counts_and_a_subnet_of_one_does(self):
        for ranges in (
            ["192.0.2.0/24"], ["198.51.100.0/24"], ["203.0.113.0/24"],
            ["203.0.113.64/26"], ["198.51.100.7/32"],
            ["2001:db8::/32"], ["2001:db8:1::/48"], ["100::/64"],
            ["192.0.2.0/24", "203.0.113.0/24"],
        ):
            with self.subTest(ranges=ranges):
                self.assertEqual(self.restricted(ranges)["severity"], "minor")

    def test_one_real_range_beside_a_documentation_one_stays_major(self):
        # The claim is that the allowlist admits nobody. One routable entry
        # falsifies it, whatever else is in the list.
        hit = self.restricted(["192.0.2.0/24", "35.191.0.0/16"])
        self.assertEqual(hit["severity"], "major")
        self.assertNotIn("documentation prefix", hit["excerpt"])

    def test_a_private_range_is_not_a_documentation_range(self):
        # RFC 1918 addresses real hosts, and one allowlisted on an external
        # load balancer may be admitting traffic that arrives over Interconnect
        # or a VPN with its source intact. `ipaddress.is_private` is true of
        # both these and the documentation blocks, which is why this check does
        # not use it.
        for ranges in (["10.0.0.0/8"], ["192.168.1.0/24"], ["172.16.0.0/12"]):
            with self.subTest(ranges=ranges):
                hit = self.restricted(ranges)
                self.assertEqual(hit["severity"], "major")
                self.assertNotIn("documentation prefix", hit["excerpt"])

    def test_a_default_route_is_not_a_restriction(self):
        # `0.0.0.0/0` is the whole internet written as an allowlist. Reading
        # the field's presence rather than its contents would downgrade every
        # one of these to `major`.
        for ranges in (["0.0.0.0/0"], ["::/0"], ["35.191.0.0/16", "0.0.0.0/0"]):
            with self.subTest(ranges=ranges):
                hit = self.restricted(ranges)
                self.assertIsNone(hit.get("severity"))
                self.assertNotIn("admits", hit["excerpt"])

    def test_an_unreadable_range_leaves_the_severity_alone(self):
        # An allowlist the collector cannot parse is one it cannot vouch for,
        # and guessing in the other direction downgrades a live exposure.
        for ranges in (["not-a-cidr"], ["35.191.0.0/16", "10.0.0.0/8/8"]):
            with self.subTest(ranges=ranges):
                self.assertIsNone(self.restricted(ranges).get("severity"))

    def test_blank_entries_do_not_defeat_the_allowlist(self):
        # A blank string is not a CIDR and widens nothing, so the ranges
        # beside it still restrict. Counting it would also misreport the total.
        hit = self.restricted(["35.191.0.0/16", "", "  "])
        self.assertEqual(hit["severity"], "major")
        self.assertIn("admits 1 CIDR,", hit["excerpt"])

    def test_an_absent_field_leaves_the_severity_alone(self):
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"})
        svc = ai_service("vllm-svc", selector={"app": "vllm"}, ingress=["136.70.153.197"])
        self.assertIsNone(self.result(svc, [w])[0].get("severity"))


class TestAiSecurityCollectCluster(unittest.TestCase):
    """One end-to-end pass over ai-security-audit's real collection plan --
    a workload dump and a Service dump, joined for one check and read alone
    by the other five."""

    CLUSTER = {"name": "prod-usc1", "project": "acme", "location": "us-central1", "autopilot": False}

    def run_with(self, workload_items=(), service_items=()):
        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return Run(argv, 0, "", "", 0.05)
            if argv[:2] == ["kubectl", "get"]:
                if argv[2] == collect.COMPLIANCE_DUMP_KINDS:
                    return Run(argv, 0, json.dumps(dump_of(*workload_items)), "", 0.1)
                if argv[2] == "svc":
                    return Run(argv, 0, json.dumps(dump_of(*service_items)), "", 0.1)
            return Run(argv, 0, "", "", 0.01)

        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), patch.object(collect, "SCRATCH_DIR", tmp):
                return collect.collect_cluster(self.CLUSTER, "ai-security-audit", collect.AI_SECURITY_CHECKS, run=run)

    def test_a_cluster_with_no_ai_workloads_still_runs_every_check(self):
        """The one stream where an empty scope is *not* declared inapplicable.

        `collect_cluster` declares a target's workload-scoped checks
        inapplicable when nothing survives the filters, because a check with
        nothing to examine has cleared nothing. `_EMPTY_SCOPE_REASON` leaves
        this stream out on purpose: its filter is the subject definition
        itself, so with no AI workload on the cluster there is no object any
        of the six checks could be true of, and "ran and matched nothing" is
        a verdict about the cluster rather than a gap. The comment above
        `_EMPTY_SCOPE_REASON` argues it, and says why the older mechanical
        argument (an empty `commands` forcing a `limitations` note) was wrong.
        """
        result = self.run_with(workload_items=[deployment("web")], service_items=[])
        self.assertEqual(result["outcome"], "collected")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(len(result["commands"]), 6)
        self.assertNotIn("checks_not_applicable", result)

    def test_a_cluster_scoped_candidate_carries_the_reconciler_through_emit(self):
        """The other half of `TestClusterCheckReconcilers`: the check puts the
        field on its hit, and `emit` has to copy it onto the candidate. It did
        not until 2026-09-06 -- the guard read `workload.get("reconciler")`
        and `workload` is None on this path, so the field the check supplied
        was dropped between the two."""
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"})
        svc = marked(ai_service("vllm-svc", selector={"app": "vllm"}, ingress=["136.70.153.197"]), ARGO_TRACKING)
        result = self.run_with(workload_items=[w], service_items=[svc])
        endpoint = [c for c in result["candidates"] if c["check"] == "inference-endpoint-public"]
        self.assertEqual(len(endpoint), 1)
        self.assertEqual(endpoint[0]["reconciler"], ARGO_PHRASE)

    def test_an_unreconciled_endpoint_omits_the_key_rather_than_carrying_none(self):
        """`disclose_reconciled_manual_remediations` reads the field with a
        truth test, but the candidate is also what `derive_finding_id` and the
        ledger diff hash over: a `reconciler: null` key on every unmanaged
        object is churn in the stored manifest for no reader's benefit."""
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"})
        svc = ai_service("vllm-svc", selector={"app": "vllm"}, ingress=["136.70.153.197"])
        result = self.run_with(workload_items=[w], service_items=[svc])
        endpoint = [c for c in result["candidates"] if c["check"] == "inference-endpoint-public"]
        self.assertNotIn("reconciler", endpoint[0])

    def test_a_dirty_cluster_reports_across_both_sources(self):
        w = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"}, container={"args": ["--trust-remote-code"], "image": "acme/vllm:latest"})
        svc = ai_service("vllm-svc", selector={"app": "vllm"})
        result = self.run_with(workload_items=[w], service_items=[svc])
        slugs = {c["check"] for c in result["candidates"]}
        self.assertIn("model-remote-code-trusted", slugs)
        self.assertIn("model-image-floating-tag", slugs)
        self.assertIn("inference-endpoint-public", slugs)

    def test_a_suspended_cronjobs_candidate_says_so_in_its_excerpt(self):
        """The 2026-09-05 report told an operator that
        `CronJob/ai-batch-finetune` pulls an unpinned model and that "the bytes
        that arrive at the next pod restart are whatever the source serves
        then". That CronJob has `spec.suspend: true`, an empty
        `status.lastScheduleTime`, and has never produced a Job. The detection
        was right; the tense was not, and the excerpt is what the impact gets
        written from."""
        c = ai_workload("CronJob", "ai-batch-finetune", container={"args": ["--model", "meta-llama/Llama-3.1-8B-Instruct"]})
        c["spec"]["suspend"] = True
        result = self.run_with(workload_items=[c])
        hits = [x for x in result["candidates"] if x["check"] == "model-artifact-unpinned-source"]
        self.assertEqual(len(hits), 1)
        self.assertIn("spec.suspend=true", hits[0]["excerpt"])
        self.assertIn("still what runs when it is", hits[0]["excerpt"])

    def test_an_unsuspended_workloads_candidate_gains_nothing(self):
        w = ai_workload("Deployment", "vllm", container={"image": "acme/vllm:latest"})
        result = self.run_with(workload_items=[w])
        hits = [x for x in result["candidates"] if x["check"] == "model-image-floating-tag"]
        self.assertEqual(len(hits), 1)
        self.assertNotIn("suspend", hits[0]["excerpt"])
        self.assertNotIn("scaled to zero", hits[0]["excerpt"])

    def test_a_scaled_to_zero_workloads_candidate_says_so_in_its_excerpt(self):
        """The 2026-09-06 08:55Z report told an operator that
        `Deployment/ai-embeddings-tei` "executes arbitrary code shipped inside
        the model repository, with this pod's ServiceAccount, network access,
        and mounted volumes", and to confirm the fix with `kubectl logs
        deploy/ai-embeddings-tei`. That Deployment is at `replicas: 0`: there is
        no pod, nothing is mounted, and the verification command returns
        nothing."""
        w = ai_workload("Deployment", "ai-embeddings-tei", container={"env": [{"name": "TRUST_REMOTE_CODE", "value": "true"}]})
        w["spec"]["replicas"] = 0
        result = self.run_with(workload_items=[w])
        hits = [x for x in result["candidates"] if x["check"] == "model-remote-code-trusted"]
        self.assertEqual(len(hits), 1)
        self.assertIn("spec.replicas=0", hits[0]["excerpt"])
        self.assertIn("Deployment is scaled to zero", hits[0]["excerpt"])
        self.assertIn("commands that read one return nothing", hits[0]["excerpt"])

    def test_the_scaled_to_zero_note_names_the_kind_it_found(self):
        """Unlike suspension, this reaches two kinds, so a hardcoded noun would
        be wrong half the time."""
        w = ai_workload("StatefulSet", "vllm", container={"image": "acme/vllm:latest"})
        w["spec"]["replicas"] = 0
        result = self.run_with(workload_items=[w])
        hits = [x for x in result["candidates"] if x["check"] == "model-image-floating-tag"]
        self.assertEqual(len(hits), 1)
        self.assertIn("StatefulSet is scaled to zero", hits[0]["excerpt"])

    def test_a_scaled_to_zero_service_candidate_never_gains_it(self):
        """`inference-endpoint-public` emits on a Service, which has no
        `replicas` and no workload to read one from."""
        d = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"})
        d["spec"]["replicas"] = 0
        svc = ai_service("vllm-svc", selector={"app": "vllm"})
        result = self.run_with(workload_items=[d], service_items=[svc])
        hits = [x for x in result["candidates"] if x["check"] == "inference-endpoint-public"]
        self.assertEqual(len(hits), 1)
        self.assertNotIn("scaled to zero", hits[0]["excerpt"])

    def test_a_service_candidate_never_gains_it(self):
        """`inference-endpoint-public` emits on a Service, which has no
        `suspend` field and no workload to read one from -- the cluster-kind
        arm of `emit` must not reach for one."""
        c = ai_workload("CronJob", "batch", pod_labels={"app": "vllm"})
        c["spec"]["suspend"] = True
        d = ai_workload("Deployment", "vllm", pod_labels={"app": "vllm"})
        svc = ai_service("vllm-svc", selector={"app": "vllm"})
        result = self.run_with(workload_items=[c, d], service_items=[svc])
        hits = [x for x in result["candidates"] if x["check"] == "inference-endpoint-public"]
        self.assertEqual(len(hits), 1)
        self.assertNotIn("suspend", hits[0]["excerpt"])

    def test_a_gate_failure_on_the_service_dump_fails_the_whole_cluster(self):
        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return Run(argv, 0, "", "", 0.05)
            if argv[:2] == ["kubectl", "get"] and argv[2] == collect.COMPLIANCE_DUMP_KINDS:
                return Run(argv, 0, json.dumps(dump_of()), "", 0.1)
            if argv[:2] == ["kubectl", "get"] and argv[2] == "svc":
                return Run(argv, 1, "", "RBAC forbidden", 0.1)
            return Run(argv, 0, "", "", 0.01)

        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), patch.object(collect, "SCRATCH_DIR", tmp):
                result = collect.collect_cluster(self.CLUSTER, "ai-security-audit", collect.AI_SECURITY_CHECKS, run=run)
        self.assertEqual(result["outcome"], "gate-failed")
        self.assertNotIn("candidates", result)

    def test_a_downgraded_credential_candidate_is_impact_authoritative(self):
        """End to end, because the unit arm passing is not what went wrong.

        `emit` derives the flag from `hit["impact"]` alone, so a hit that set
        only `severity` published the spec's `major` sentence on a `minor`
        finding *and* left the flag unset -- which means `adopt_arm_impact`
        could not correct it and `carry_unchanged_findings` would republish it
        every run. Both halves have to hold at the candidate.
        """
        w = ai_workload(
            "Deployment",
            "vllm",
            container={"env": [{"name": "HF_TOKEN", "value": "hf_EXAMPLE_PLACEHOLDER_NOT_A_REAL_TOKEN"}]},
        )
        result = self.run_with(workload_items=[w], service_items=[])
        cand = next(c for c in result["candidates"] if c["check"] == "model-credential-plaintext-env")
        self.assertEqual(cand["severity"], "minor")
        self.assertIs(cand["impact_authoritative"], True)
        self.assertNotIn("is embedded", cand["impact"])

    def test_the_service_dump_backs_only_inference_endpoint_public(self):
        result = self.run_with(workload_items=[deployment("web")], service_items=[])
        commands_by_slug = {c["check"]: c["command"] for c in result["commands"]}
        self.assertIn(" svc ", commands_by_slug["inference-endpoint-public"])
        for slug in commands_by_slug:
            if slug != "inference-endpoint-public":
                self.assertIn(collect.COMPLIANCE_DUMP_KINDS, commands_by_slug[slug])


class TestEvidenceCommandsArePasteable(unittest.TestCase):
    """Every published command is an offer: run this and see what we saw.

    The ledger says so in as many words — "these are re-runnable so that it
    does not have to be taken on trust". A command that a shell refuses is
    worse than no command at all, because the reader concludes the finding is
    junk rather than the rendering. `compliance-audit` shipped seventeen
    criticals on 2026-08-29 citing

        gcloud ... --format json(workloadIdentityConfig,privateClusterConfig,...)

    which answers `Syntax error: "(" unexpected`, rc=2 — the argv was correct
    and only the space-join that rendered it was not.
    """

    MODULES = ("collect", "fleet_drift", "patch_readiness")

    def test_no_collector_renders_an_argv_by_space_joining_it(self):
        # A behavioural test can only reach the argvs some fixture happens to
        # provoke. This reaches all of them, including the ones no test builds
        # yet, and it is the check that fails when the next site is added.
        pattern = re.compile(r"""["'] ["']\.join\((\w*argv|cmd)\)""")
        for name in self.MODULES:
            with self.subTest(module=name):
                source = (Path(__file__).resolve().parent / f"{name}.py").read_text()
                self.assertEqual(
                    pattern.findall(source),
                    [],
                    f"{name}.py renders an argv with a space-join; use shlex.join",
                )

    def test_the_compliance_describe_command_survives_a_shell(self):
        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), patch.object(collect, "SCRATCH_DIR", tmp):
                result = collect.collect_cluster(
                    {"name": "kube-agents-host", "location": "us-east4", "project": "adamparco-kage"},
                    "compliance-audit",
                    collect.COMPLIANCE_CHECKS,
                    run=self._run,
                )
        commands = {c["check"]: c["command"] for c in result["commands"]}
        rendered = commands["public-control-plane"]
        self.assertIn("--format", rendered)
        # The whole `json(...)` selector arrives as one word, parentheses and
        # all, rather than as a subshell the shell then tries to open.
        selector = shlex.split(rendered)[shlex.split(rendered).index("--format") + 1]
        self.assertTrue(selector.startswith("json(") and selector.endswith(")"), selector)
        self.assertEqual(shlex.split(rendered), self.describe_argv)

    def setUp(self):
        self.describe_argv = None

    def _run(self, argv, **kwargs):
        if argv[:4] == ["gcloud", "container", "clusters", "describe"]:
            self.describe_argv = list(argv)
            return Run(argv, 0, json.dumps({"privateClusterConfig": {}}), "", 0.1)
        if argv[:2] == ["kubectl", "get"] and argv[2] == collect.COMPLIANCE_DUMP_KINDS:
            return Run(argv, 0, json.dumps(dump_of()), "", 0.1)
        if argv[:2] == ["gcloud", "container"] and argv[2] == "node-pools":
            return Run(argv, 0, "[]", "", 0.1)
        return Run(argv, 0, json.dumps({"items": []}), "", 0.05)

    def test_an_empty_compliance_scope_declares_the_automount_checks_inapplicable(self):
        # Both automount checks loop over the workload set, so with nothing in
        # scope they examined nothing, exactly as a workload check did.
        # netpol-missing reads the namespaces holding live Pods instead and
        # still ran.
        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), patch.object(collect, "SCRATCH_DIR", tmp):
                result = collect.collect_cluster(
                    {"name": "c1", "location": "us-east4", "project": "p"},
                    "compliance-audit",
                    collect.COMPLIANCE_CHECKS,
                    run=self._run,
                )
        na = {e["check"] for e in result.get("checks_not_applicable") or []}
        self.assertLessEqual({"default-sa-automount", "unbound-sa-automount"}, na)
        self.assertNotIn("netpol-missing", na)
        self.assertIn("netpol-missing", {c["check"] for c in result["commands"]})
        self.assertFalse(na & {c["check"] for c in result["commands"]})



class TestWorkloadDeclarations(unittest.TestCase):
    """The index that decides whether a finding can travel in a pull request.

    Every case here is a shape the live fleet actually produced on 2026-09-06,
    when 14 findings published as `kind: manual` -- "no pull request is
    possible" -- on objects the GitOps repository declares, and one object
    (`Deployment/waste-unsized`) was `manifest` under one check and `manual`
    under three others in the same run.
    """

    DEPLOYMENT = textwrap.dedent(
        """\
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: waste-unsized
          namespace: waste-canary
        spec:
          replicas: 1
        """
    )

    def tree(self, tmp, files):
        for relative, text in files.items():
            path = Path(tmp) / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return Path(tmp)

    def test_an_object_under_its_cluster_tree_resolves_to_its_file(self):
        with TemporaryDirectory() as tmp:
            root = self.tree(
                tmp,
                {"clusters/spot-capacity-test/workloads/fixture.yaml": self.DEPLOYMENT},
            )
            index = collect.workload_declarations(root)
            found = collect.declaration_for(
                index, "spot-capacity-test", "waste-canary", "Deployment/waste-unsized"
            )
        self.assertEqual(
            found,
            {
                "path": "clusters/spot-capacity-test/workloads/fixture.yaml",
                "directory": "clusters/spot-capacity-test/workloads",
            },
        )

    def test_the_directory_is_where_a_new_sibling_object_would_go(self):
        """`no-pdb` creates a PDB beside the workload rather than editing it.

        The live run got this branch right on its own -- it named a new
        `waste-unsized-pdb.yaml` next to the Deployment -- so the index must
        hand back the parent directory as well as the file, or annotating the
        candidate would argue the model out of a correct answer.
        """
        with TemporaryDirectory() as tmp:
            root = self.tree(
                tmp,
                {"clusters/spot-capacity-test/workloads/fixture.yaml": self.DEPLOYMENT},
            )
            index = collect.workload_declarations(root)
            found = collect.declaration_for(
                index, "spot-capacity-test", "waste-canary", "Deployment/waste-unsized"
            )
        self.assertEqual(found["directory"], "clusters/spot-capacity-test/workloads")

    def test_another_clusters_tree_is_not_a_match(self):
        """The failure the SOPs warn `grep` has, and the reason for exact keys.

        A namespaced object written into the wrong cluster's applied tree fails
        that tree's sync, so resolving nothing is the better answer.
        """
        with TemporaryDirectory() as tmp:
            root = self.tree(
                tmp, {"clusters/some-other-cluster/workloads/fixture.yaml": self.DEPLOYMENT}
            )
            index = collect.workload_declarations(root)
            found = collect.declaration_for(
                index, "spot-capacity-test", "waste-canary", "Deployment/waste-unsized"
            )
        self.assertIsNone(found)

    def test_the_same_name_under_a_different_kind_is_not_a_match(self):
        """`grep -rl "name: <object>"` is kind-blind; this index is not."""
        with TemporaryDirectory() as tmp:
            root = self.tree(
                tmp,
                {
                    "clusters/spot-capacity-test/workloads/fixture.yaml": self.DEPLOYMENT.replace(
                        "kind: Deployment", "kind: StatefulSet"
                    )
                },
            )
            index = collect.workload_declarations(root)
            found = collect.declaration_for(
                index, "spot-capacity-test", "waste-canary", "Deployment/waste-unsized"
            )
        self.assertIsNone(found)

    def test_a_label_line_carrying_the_name_is_not_a_match(self):
        """The other half of the grep failure: `app.kubernetes.io/name:` lines."""
        labelled = textwrap.dedent(
            """\
            apiVersion: apps/v1
            kind: Deployment
            metadata:
              name: something-else
              namespace: waste-canary
              labels:
                app.kubernetes.io/name: waste-unsized
            """
        )
        with TemporaryDirectory() as tmp:
            root = self.tree(tmp, {"clusters/spot-capacity-test/workloads/f.yaml": labelled})
            index = collect.workload_declarations(root)
            found = collect.declaration_for(
                index, "spot-capacity-test", "waste-canary", "Deployment/waste-unsized"
            )
        self.assertIsNone(found)

    def test_a_different_namespace_is_not_a_match(self):
        with TemporaryDirectory() as tmp:
            root = self.tree(tmp, {"clusters/spot-capacity-test/workloads/f.yaml": self.DEPLOYMENT})
            index = collect.workload_declarations(root)
            found = collect.declaration_for(
                index, "spot-capacity-test", "other-namespace", "Deployment/waste-unsized"
            )
        self.assertIsNone(found)

    def test_two_files_declaring_one_object_resolve_to_nothing(self):
        """A duplicate resource id Argo and Config Sync both reject.

        Naming either file would be right half the time; `manual` is right
        every time.
        """
        with TemporaryDirectory() as tmp:
            root = self.tree(
                tmp,
                {
                    "clusters/spot-capacity-test/workloads/a.yaml": self.DEPLOYMENT,
                    "clusters/spot-capacity-test/workloads/b.yaml": self.DEPLOYMENT,
                },
            )
            index = collect.workload_declarations(root)
            found = collect.declaration_for(
                index, "spot-capacity-test", "waste-canary", "Deployment/waste-unsized"
            )
        self.assertIsNone(found)

    def test_a_config_connector_resource_is_left_to_the_kcc_index(self):
        """`audit_report.kcc_declarations` owns these, keyed by `spec.resourceID`.

        A `Cluster/<name>` finding must keep resolving through that table, so
        indexing the same object here would put two answers in play for one
        finding.
        """
        kcc = textwrap.dedent(
            """\
            apiVersion: container.cnrm.cloud.google.com/v1beta1
            kind: ContainerCluster
            metadata:
              name: drift-peer-std-4
              namespace: config-control
            """
        )
        with TemporaryDirectory() as tmp:
            root = self.tree(tmp, {"clusters/drift-peer-std-4/cluster.yaml": kcc})
            index = collect.workload_declarations(root)
        self.assertEqual(index, {})

    def test_a_file_outside_any_cluster_tree_is_not_indexed(self):
        """`gcp/`, `bootstrap/` and the repo root apply to no single cluster."""
        with TemporaryDirectory() as tmp:
            root = self.tree(
                tmp,
                {
                    "bootstrap/thing.yaml": self.DEPLOYMENT,
                    "gcp/thing.yaml": self.DEPLOYMENT,
                    "top.yaml": self.DEPLOYMENT,
                },
            )
            index = collect.workload_declarations(root)
        self.assertEqual(index, {})

    def test_an_unparseable_file_does_not_sink_the_index(self):
        with TemporaryDirectory() as tmp:
            root = self.tree(
                tmp,
                {
                    "clusters/spot-capacity-test/workloads/broken.yaml": "a: [unterminated\n",
                    "clusters/spot-capacity-test/workloads/good.yaml": self.DEPLOYMENT,
                },
            )
            index = collect.workload_declarations(root)
            found = collect.declaration_for(
                index, "spot-capacity-test", "waste-canary", "Deployment/waste-unsized"
            )
        self.assertEqual(found["path"], "clusters/spot-capacity-test/workloads/good.yaml")

    def test_an_object_the_repository_does_not_declare_resolves_to_nothing(self):
        """The 47 `kube-agents-host` findings on this fleet: argocd and
        cert-manager are installed by Helm, and no pull request against this
        repository can edit them."""
        with TemporaryDirectory() as tmp:
            root = self.tree(tmp, {"clusters/spot-capacity-test/workloads/f.yaml": self.DEPLOYMENT})
            index = collect.workload_declarations(root)
            found = collect.declaration_for(
                index, "kube-agents-host", "argocd", "Deployment/argocd-server"
            )
        self.assertIsNone(found)

    def test_a_malformed_object_reference_resolves_to_nothing(self):
        with TemporaryDirectory() as tmp:
            root = self.tree(tmp, {"clusters/spot-capacity-test/workloads/f.yaml": self.DEPLOYMENT})
            index = collect.workload_declarations(root)
            for obj in ("", "waste-unsized", "/waste-unsized", "Deployment/"):
                with self.subTest(object=obj):
                    self.assertIsNone(
                        collect.declaration_for(
                            index, "spot-capacity-test", "waste-canary", obj
                        )
                    )

    def test_an_unreadable_clone_yields_an_empty_index(self):
        """Empty means unannotated, which leaves the SOP's grep as the answer."""
        self.assertEqual(collect.workload_declarations(Path("/nonexistent-clone")), {})


class TestCandidatesCarryTheirDeclaration(unittest.TestCase):
    """The annotation has to reach the candidate, or the model never sees it.

    `workload_declarations` and `declaration_for` are unit-tested above
    against a clone on disk. What those tests cannot show is that the answer
    survives the trip to a candidate: the index is built in `collect_fleet`,
    handed across a thread pool to `collect_cluster`, and attached inside
    `emit`, and a break at any of those three joints leaves the index correct
    and every candidate unannotated -- which is indistinguishable, from the
    model's side, from a fleet whose objects are genuinely undeclared. So
    these drive the real `--workspace` path end to end.
    """

    CLUSTER = {"name": "prod-usc1", "location": "us-central1", "status": "RUNNING"}
    DECLARED = "clusters/prod-usc1/workloads/api.yaml"

    def clone(self, tmp):
        """A GitOps tree declaring Deployment/api, and nothing else.

        `web` is deliberately absent: one run then covers both arms, so a
        change that annotates unconditionally fails here rather than passing
        a test that only ever looks at declared objects.
        """
        path = Path(tmp) / "infra"
        (path / "clusters/prod-usc1/workloads").mkdir(parents=True)
        (path / self.DECLARED).write_text(
            textwrap.dedent(
                """
                apiVersion: apps/v1
                kind: Deployment
                metadata:
                  name: api
                  namespace: default
                """
            ).strip()
        )
        return path

    def fleet_run(self, *names):
        """`gcloud container clusters list` answers one cluster; every
        `kubectl get` answers the same workloads. Deployments with no
        resources and no probes trip several obtainability checks, so each
        name yields more than one candidate.
        """

        def run(argv, **kwargs):
            if argv[:4] == ["gcloud", "container", "clusters", "list"]:
                return Run(argv, 0, json.dumps([self.CLUSTER]), "", 0.01)
            if argv[:2] == ["kubectl", "get"]:
                return Run(argv, 0, json.dumps(dump_of(*(deployment(n) for n in names))), "", 0.01)
            return Run(argv, 0, "", "", 0.01)

        return run

    def candidates(self, manifest):
        return [c for cluster in manifest["clusters"] for c in cluster.get("candidates") or []]

    def test_a_declared_object_carries_its_path_and_directory(self):
        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), \
                    patch.object(collect, "SCRATCH_DIR", tmp):
                manifest = collect.collect_fleet(
                    "obtainability-audit", "acme",
                    run=self.fleet_run("api", "web"), workspace=self.clone(tmp),
                )

        candidates = self.candidates(manifest)
        api = [c for c in candidates if c["object"] == "Deployment/api"]
        web = [c for c in candidates if c["object"] == "Deployment/web"]
        # Guard the guard: an empty list would satisfy every assertion below.
        self.assertTrue(api, "no candidate for the declared workload")
        self.assertTrue(web, "no candidate for the undeclared workload")

        for candidate in api:
            self.assertEqual(
                candidate["declaration"],
                {"path": self.DECLARED, "directory": "clusters/prod-usc1/workloads"},
                candidate["check"],
            )
        # `web` is in the same dump, same cluster, same namespace, and differs
        # only in being absent from the clone.
        for candidate in web:
            self.assertNotIn("declaration", candidate, candidate["check"])

    def test_the_path_is_relative_to_the_clone_not_the_filesystem(self):
        """What ships is what a PR branch has to check out.

        An absolute path leaks the agent's scratch directory into the ledger
        and into the remediation the model writes, where it names a file the
        GitOps repository does not contain.
        """
        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), \
                    patch.object(collect, "SCRATCH_DIR", tmp):
                manifest = collect.collect_fleet(
                    "obtainability-audit", "acme",
                    run=self.fleet_run("api"), workspace=self.clone(tmp),
                )

        for candidate in self.candidates(manifest):
            declaration = candidate.get("declaration")
            if declaration:
                self.assertFalse(Path(declaration["path"]).is_absolute())
                self.assertNotIn(tmp, declaration["path"])

    def test_a_workspace_that_declares_nothing_annotates_nothing(self):
        """An empty clone is not an error, and must not be a wrong answer.

        A checkout that failed, or a repository whose layout does not match
        `clusters/<name>/`, yields an empty index -- and the SOPs say an
        absent annotation is "not a claim that no declaration exists", so the
        model falls back to its grep rather than being told the object is
        undeclared.
        """
        with TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty"
            empty.mkdir()
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), \
                    patch.object(collect, "SCRATCH_DIR", tmp):
                manifest = collect.collect_fleet(
                    "obtainability-audit", "acme", run=self.fleet_run("api"), workspace=empty,
                )

        candidates = self.candidates(manifest)
        self.assertTrue(candidates)
        for candidate in candidates:
            self.assertNotIn("declaration", candidate)

    def test_without_a_workspace_no_candidate_is_annotated(self):
        def run(argv, **kwargs):
            return Run(argv, 0, "[]", "", 0.01)

        manifest = collect.collect_fleet("obtainability-audit", "acme", run=run)
        for cluster in manifest["clusters"]:
            for candidate in cluster.get("candidates") or []:
                self.assertNotIn("declaration", candidate)

    def test_without_a_workspace_a_populated_fleet_is_still_unannotated(self):
        """The test above lists an empty fleet, so it never reaches `emit`.

        Omitting `--workspace` has to stay the no-op it was before this flag
        existed, on a run that actually produces candidates.
        """
        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), \
                    patch.object(collect, "SCRATCH_DIR", tmp):
                manifest = collect.collect_fleet(
                    "obtainability-audit", "acme", run=self.fleet_run("api"),
                )

        candidates = self.candidates(manifest)
        self.assertTrue(candidates)
        for candidate in candidates:
            self.assertNotIn("declaration", candidate)


class TestReleaseOf(unittest.TestCase):
    """The structured half of `reconciler_of`, which is what the index needs."""

    def test_the_helm_annotation_pair_becomes_release_coordinates(self):
        release = collect.release_of(
            {
                "annotations": {
                    "meta.helm.sh/release-name": "cert-manager",
                    "meta.helm.sh/release-namespace": "cert-manager",
                }
            }
        )
        self.assertEqual(
            release, {"namespace": "cert-manager", "name": "cert-manager", "application": ""}
        )

    def test_an_argocd_tracking_id_yields_the_application_name(self):
        """The id is `<app>:<group>/<Kind>:<ns>/<name>`; only the head is the app."""
        release = collect.release_of(
            {
                "annotations": {
                    "argocd.argoproj.io/tracking-id": "cert-manager:apps/Deployment:cert-manager/cert-manager"
                }
            }
        )
        self.assertEqual(release, {"namespace": "", "name": "", "application": "cert-manager"})

    def test_both_markers_are_kept_rather_than_chosen_between(self):
        release = collect.release_of(
            {
                "annotations": {
                    "meta.helm.sh/release-name": "redis",
                    "meta.helm.sh/release-namespace": "data",
                    "argocd.argoproj.io/tracking-id": "redis-app:apps/StatefulSet:data/redis",
                }
            }
        )
        self.assertEqual(
            release, {"namespace": "data", "name": "redis", "application": "redis-app"}
        )

    def test_an_object_no_chart_installs_has_no_release(self):
        self.assertIsNone(collect.release_of({"annotations": {}}))
        self.assertIsNone(collect.release_of({}))

    def test_the_managed_by_label_alone_is_not_a_release(self):
        """`app.kubernetes.io/managed-by: Helm` names no release to look up.

        `reconciler_of` can still say something useful with it; this cannot,
        and a key built from it would be `("", "")`.
        """
        self.assertIsNone(collect.release_of({"labels": {"app.kubernetes.io/managed-by": "Helm"}}))


class TestReleaseDeclarations(unittest.TestCase):
    """Resolving a chart-rendered workload to the release its repo declares.

    `workload_declarations` above finds the file declaring an object. A
    workload a chart renders has no such file, which is why 48 of the 53
    findings in the 2026-09-07 obtainability run were `manual`. These cover the
    declaration shapes that carry a values override instead.
    """

    APPLICATION = textwrap.dedent(
        """\
        apiVersion: argoproj.io/v1alpha1
        kind: Application
        metadata:
          name: cert-manager
          namespace: argocd
        spec:
          source:
            repoURL: https://charts.jetstack.io
            chart: cert-manager
            targetRevision: v1.14.4
          destination:
            name: prod-usc1
            namespace: cert-manager
        """
    )

    def tree(self, tmp, files):
        for relative, text in files.items():
            path = Path(tmp) / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return Path(tmp)

    def resolve(self, files, cluster, release):
        with TemporaryDirectory() as tmp:
            index = collect.release_declarations(self.tree(tmp, files))
        return collect.release_declaration_for(index, cluster, release)

    def test_an_argocd_chart_application_resolves_from_its_tracking_id(self):
        found = self.resolve(
            {"apps/cert-manager.yaml": self.APPLICATION},
            "prod-usc1",
            {"application": "cert-manager", "name": "", "namespace": ""},
        )
        self.assertEqual(
            found,
            {
                "path": "apps/cert-manager.yaml",
                "kind": "Application",
                "renderer": "helm",
                "chart": "cert-manager",
                "repo": "https://charts.jetstack.io",
                "version": "v1.14.4",
                "values_field": "spec.source.helm.valuesObject",
            },
        )

    def test_the_same_application_resolves_from_the_release_coordinates(self):
        """Argo CD driving Helm through a plugin leaves the `meta.helm.sh` pair.

        The Application is registered under both keys, so either marker on the
        workload finds it.
        """
        found = self.resolve(
            {"apps/cert-manager.yaml": self.APPLICATION},
            "prod-usc1",
            {"application": "", "name": "cert-manager", "namespace": "cert-manager"},
        )
        self.assertIsNotNone(found)
        self.assertEqual(found["path"], "apps/cert-manager.yaml")

    def test_an_application_outside_any_cluster_tree_still_resolves(self):
        """The reason this cannot reuse `workload_declarations`' path rule.

        An Application names its own destination, and repositories keep them in
        `apps/`, `bootstrap/`, or the root -- none of which is
        `clusters/<name>/`. Keying off the path would index none of them.
        """
        found = self.resolve(
            {"cert-manager-app.yaml": self.APPLICATION},
            "prod-usc1",
            {"application": "cert-manager", "name": "", "namespace": ""},
        )
        self.assertIsNotNone(found)
        self.assertEqual(found["path"], "cert-manager-app.yaml")

    def test_a_destination_server_resolves_through_the_cluster_registration(self):
        application = self.APPLICATION.replace(
            "    name: prod-usc1\n", "    server: https://34.21.99.255\n"
        )
        secret = textwrap.dedent(
            """\
            apiVersion: v1
            kind: Secret
            metadata:
              name: cluster-prod-usc1
              namespace: argocd
              labels:
                argocd.argoproj.io/secret-type: cluster
            stringData:
              name: prod-usc1
              server: https://34.21.99.255
            """
        )
        found = self.resolve(
            {"apps/cert-manager.yaml": application, "argocd/clusters/prod-usc1.yaml": secret},
            "prod-usc1",
            {"application": "cert-manager", "name": "", "namespace": ""},
        )
        self.assertIsNotNone(found)
        self.assertEqual(found["chart"], "cert-manager")

    def test_a_server_no_registration_names_resolves_to_nothing(self):
        """Better unannotated than resolved into another cluster's tree."""
        application = self.APPLICATION.replace(
            "    name: prod-usc1\n", "    server: https://34.21.99.255\n"
        )
        found = self.resolve(
            {"apps/cert-manager.yaml": application},
            "prod-usc1",
            {"application": "cert-manager", "name": "", "namespace": ""},
        )
        self.assertIsNone(found)

    def test_the_in_cluster_destination_is_not_resolved(self):
        """`https://kubernetes.default.svc` is whichever cluster Argo runs on.

        Nothing in the repository says which that is, so a finding on any
        cluster would otherwise match it.
        """
        application = self.APPLICATION.replace(
            "    name: prod-usc1\n", "    server: https://kubernetes.default.svc\n"
        )
        found = self.resolve(
            {"apps/cert-manager.yaml": application},
            "prod-usc1",
            {"application": "cert-manager", "name": "", "namespace": ""},
        )
        self.assertIsNone(found)

    def test_a_git_sourced_application_is_not_a_release(self):
        """The discriminator that keeps this off ordinary GitOps Applications.

        `workloads-<cluster>` syncs a directory of plain manifests, and its
        objects resolve through `workload_declarations` to their own files. A
        values override for one would be nonsense.
        """
        application = textwrap.dedent(
            """\
            apiVersion: argoproj.io/v1alpha1
            kind: Application
            metadata:
              name: workloads-prod-usc1
            spec:
              source:
                repoURL: https://github.com/acme/infra.git
                targetRevision: main
                path: clusters/prod-usc1/workloads
              destination:
                name: prod-usc1
                namespace: default
            """
        )
        found = self.resolve(
            {"bootstrap/app.yaml": application},
            "prod-usc1",
            {"application": "workloads-prod-usc1", "name": "", "namespace": ""},
        )
        self.assertIsNone(found)

    def test_an_existing_values_string_is_named_over_the_object_form(self):
        """Argo rejects an Application carrying both, so the override joins the
        block already there rather than opening a second one."""
        application = textwrap.dedent(
            """\
            apiVersion: argoproj.io/v1alpha1
            kind: Application
            metadata:
              name: cert-manager
            spec:
              source:
                repoURL: https://charts.jetstack.io
                chart: cert-manager
                targetRevision: v1.14.4
                helm:
                  values: |
                    replicaCount: 2
              destination:
                name: prod-usc1
                namespace: cert-manager
            """
        )
        found = self.resolve(
            {"apps/cert-manager.yaml": application},
            "prod-usc1",
            {"application": "cert-manager", "name": "", "namespace": ""},
        )
        self.assertIsNotNone(found)
        self.assertEqual(found["values_field"], "spec.source.helm.values")

    def test_a_multi_source_application_names_the_chart_entry_it_found(self):
        application = textwrap.dedent(
            """\
            apiVersion: argoproj.io/v1alpha1
            kind: Application
            metadata:
              name: redis
            spec:
              sources:
                - repoURL: https://github.com/acme/values.git
                  targetRevision: main
                  ref: values
                - repoURL: https://charts.bitnami.com/bitnami
                  chart: redis
                  targetRevision: 19.0.0
              destination:
                name: prod-usc1
                namespace: data
            """
        )
        found = self.resolve(
            {"apps/redis.yaml": application},
            "prod-usc1",
            {"application": "redis", "name": "", "namespace": ""},
        )
        self.assertIsNotNone(found)
        self.assertEqual(found["values_field"], "spec.sources[1].helm.valuesObject")
        self.assertEqual(found["repo"], "https://charts.bitnami.com/bitnami")

    def test_two_chart_sources_on_one_application_resolve_to_nothing(self):
        application = textwrap.dedent(
            """\
            apiVersion: argoproj.io/v1alpha1
            kind: Application
            metadata:
              name: pair
            spec:
              sources:
                - repoURL: https://charts.example.com
                  chart: one
                  targetRevision: "1.0.0"
                - repoURL: https://charts.example.com
                  chart: two
                  targetRevision: "2.0.0"
              destination:
                name: prod-usc1
                namespace: data
            """
        )
        found = self.resolve(
            {"apps/pair.yaml": application},
            "prod-usc1",
            {"application": "pair", "name": "", "namespace": ""},
        )
        self.assertIsNone(found)

    def test_a_flux_helmrelease_resolves_through_its_repository(self):
        helmrelease = textwrap.dedent(
            """\
            apiVersion: helm.toolkit.fluxcd.io/v2
            kind: HelmRelease
            metadata:
              name: podinfo
              namespace: apps
            spec:
              chart:
                spec:
                  chart: podinfo
                  version: 6.5.4
                  sourceRef:
                    kind: HelmRepository
                    name: podinfo
                    namespace: flux-system
              values:
                replicaCount: 1
            """
        )
        repository = textwrap.dedent(
            """\
            apiVersion: source.toolkit.fluxcd.io/v1
            kind: HelmRepository
            metadata:
              name: podinfo
              namespace: flux-system
            spec:
              url: https://stefanprodan.github.io/podinfo
            """
        )
        found = self.resolve(
            {
                "clusters/prod-usc1/apps/podinfo.yaml": helmrelease,
                "clusters/prod-usc1/flux/repos.yaml": repository,
            },
            "prod-usc1",
            {"application": "", "name": "podinfo", "namespace": "apps"},
        )
        self.assertEqual(
            found,
            {
                "path": "clusters/prod-usc1/apps/podinfo.yaml",
                "kind": "HelmRelease",
                "renderer": "helm",
                "chart": "podinfo",
                "repo": "https://stefanprodan.github.io/podinfo",
                "version": "6.5.4",
                "values_field": "spec.values",
            },
        )

    def test_a_helmrelease_target_namespace_overrides_its_own(self):
        helmrelease = textwrap.dedent(
            """\
            apiVersion: helm.toolkit.fluxcd.io/v2
            kind: HelmRelease
            metadata:
              name: podinfo
              namespace: flux-system
            spec:
              targetNamespace: apps
              releaseName: podinfo-prod
              chart:
                spec:
                  chart: podinfo
                  version: 6.5.4
            """
        )
        files = {"clusters/prod-usc1/apps/podinfo.yaml": helmrelease}
        self.assertIsNotNone(
            self.resolve(
                files, "prod-usc1", {"application": "", "name": "podinfo-prod", "namespace": "apps"}
            )
        )
        # The HelmRelease's own namespace is where the object lives, not where
        # the release installs, so it must not be the key.
        self.assertIsNone(
            self.resolve(
                files,
                "prod-usc1",
                {"application": "", "name": "podinfo-prod", "namespace": "flux-system"},
            )
        )

    def test_a_helmrelease_outside_a_cluster_tree_resolves_to_nothing(self):
        """Flux is per-cluster and a HelmRelease names no destination, so the
        path convention is the only cluster signal it carries."""
        helmrelease = textwrap.dedent(
            """\
            apiVersion: helm.toolkit.fluxcd.io/v2
            kind: HelmRelease
            metadata:
              name: podinfo
              namespace: apps
            spec:
              chart:
                spec:
                  chart: podinfo
                  version: 6.5.4
            """
        )
        found = self.resolve(
            {"apps/podinfo.yaml": helmrelease},
            "prod-usc1",
            {"application": "", "name": "podinfo", "namespace": "apps"},
        )
        self.assertIsNone(found)

    def test_two_declarations_of_one_release_resolve_to_nothing(self):
        duplicate = self.APPLICATION.replace("targetRevision: v1.14.4", "targetRevision: v1.15.0")
        found = self.resolve(
            {"apps/a.yaml": self.APPLICATION, "apps/b.yaml": duplicate},
            "prod-usc1",
            {"application": "cert-manager", "name": "", "namespace": ""},
        )
        self.assertIsNone(found)

    def test_an_applicationset_is_not_indexed(self):
        """Its generated names and destinations are templates this cannot
        evaluate, and guessing the cluster is the failure mode the exact match
        exists to avoid."""
        appset = textwrap.dedent(
            """\
            apiVersion: argoproj.io/v1alpha1
            kind: ApplicationSet
            metadata:
              name: charts
            spec:
              generators:
                - clusters: {}
              template:
                metadata:
                  name: "cert-manager-{{.name}}"
                spec:
                  source:
                    repoURL: https://charts.jetstack.io
                    chart: cert-manager
                    targetRevision: v1.14.4
                  destination:
                    server: "{{.server}}"
                    namespace: cert-manager
            """
        )
        found = self.resolve(
            {"apps/appset.yaml": appset},
            "prod-usc1",
            {"application": "cert-manager-prod-usc1", "name": "", "namespace": ""},
        )
        self.assertIsNone(found)

    def test_a_release_on_another_cluster_never_matches(self):
        found = self.resolve(
            {"apps/cert-manager.yaml": self.APPLICATION},
            "staging-usc1",
            {"application": "cert-manager", "name": "", "namespace": ""},
        )
        self.assertIsNone(found)

    def test_a_workload_no_chart_installs_resolves_to_nothing(self):
        with TemporaryDirectory() as tmp:
            index = collect.release_declarations(
                self.tree(tmp, {"apps/cert-manager.yaml": self.APPLICATION})
            )
        self.assertIsNone(collect.release_declaration_for(index, "prod-usc1", None))

    def test_an_empty_index_makes_no_claim(self):
        self.assertIsNone(
            collect.release_declaration_for({}, "prod-usc1", {"application": "cert-manager"})
        )

    def test_an_unreadable_clone_yields_an_empty_index(self):
        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope"
            self.assertEqual(collect.release_declarations(missing), {})

    def test_a_base64_cluster_secret_is_not_read(self):
        """A committed `data:` block would be a leaked kubeconfig, so only the
        plaintext `stringData` registration is trusted."""
        application = self.APPLICATION.replace(
            "    name: prod-usc1\n", "    server: https://34.21.99.255\n"
        )
        secret = textwrap.dedent(
            """\
            apiVersion: v1
            kind: Secret
            metadata:
              name: cluster-prod-usc1
              labels:
                argocd.argoproj.io/secret-type: cluster
            data:
              name: cHJvZC11c2Mx
              server: aHR0cHM6Ly8zNC4yMS45OS4yNTU=
            """
        )
        found = self.resolve(
            {"apps/cert-manager.yaml": application, "argocd/clusters/prod-usc1.yaml": secret},
            "prod-usc1",
            {"application": "cert-manager", "name": "", "namespace": ""},
        )
        self.assertIsNone(found)


class TestKustomizeOverlayDeclarations(unittest.TestCase):
    """The other way a workload ends up with no manifest of its own.

    An overlay over a base in another repository renders objects
    `workload_declarations` cannot find, exactly as a chart does. The override
    is a patch rather than a values key, so `renderer` says which.
    """

    OVERLAY = textwrap.dedent(
        """\
        apiVersion: argoproj.io/v1alpha1
        kind: Application
        metadata:
          name: podinfo-overlay
          namespace: argocd
        spec:
          source:
            repoURL: https://github.com/example/fleet
            path: overlays/prod-usc1/podinfo
            targetRevision: main
          destination:
            name: prod-usc1
            namespace: podinfo
        """
    )
    KUSTOMIZATION = textwrap.dedent(
        """\
        apiVersion: kustomize.config.k8s.io/v1beta1
        kind: Kustomization
        resources:
          - https://github.com/stefanprodan/podinfo//kustomize?ref=6.9.2
        """
    )
    EXPECTED = {
        "path": "apps/podinfo.yaml",
        "kind": "Application",
        "renderer": "kustomize",
        "chart": "overlays/prod-usc1/podinfo",
        "repo": "https://github.com/example/fleet",
        "version": "main",
        "values_field": "spec.source.kustomize.patches",
    }
    TRACKED = {"application": "podinfo-overlay", "name": "", "namespace": ""}

    def resolve(self, files, cluster, release):
        with TemporaryDirectory() as tmp:
            for relative, text in files.items():
                path = Path(tmp) / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            index = collect.release_declarations(Path(tmp))
        return collect.release_declaration_for(index, cluster, release)

    def test_an_overlay_resolves_to_the_application_declaring_it(self):
        found = self.resolve(
            {
                "apps/podinfo.yaml": self.OVERLAY,
                "overlays/prod-usc1/podinfo/kustomization.yaml": self.KUSTOMIZATION,
            },
            "prod-usc1",
            self.TRACKED,
        )
        self.assertEqual(found, self.EXPECTED)

    def test_the_legacy_kustomization_spellings_resolve_too(self):
        """`kustomize build` still accepts both, and a repo using one is as
        unresolvable without this as a repo using `kustomization.yaml`."""
        for filename in ("kustomization.yml", "Kustomization"):
            with self.subTest(filename=filename):
                found = self.resolve(
                    {
                        "apps/podinfo.yaml": self.OVERLAY,
                        f"overlays/prod-usc1/podinfo/{filename}": self.KUSTOMIZATION,
                    },
                    "prod-usc1",
                    self.TRACKED,
                )
                self.assertEqual(found, self.EXPECTED)

    def test_a_path_with_no_kustomization_file_is_not_an_overlay(self):
        """The plain directory of manifests `_argocd_chart_source` set aside.
        Its objects have their own YAML, which `workload_declarations` finds."""
        found = self.resolve(
            {
                "apps/podinfo.yaml": self.OVERLAY,
                "overlays/prod-usc1/podinfo/deployment.yaml": "kind: Deployment\n",
            },
            "prod-usc1",
            self.TRACKED,
        )
        self.assertIsNone(found)

    def sourced(self, entries):
        """`OVERLAY` with its single source replaced by a `sources` list."""
        body = "".join(
            "  - " + textwrap.indent(entry, "    ").lstrip() for entry in entries
        )
        return self.OVERLAY.replace(
            "  source:\n"
            "    repoURL: https://github.com/example/fleet\n"
            "    path: overlays/prod-usc1/podinfo\n"
            "    targetRevision: main\n",
            "  sources:\n" + body,
        )

    def test_a_chart_source_still_wins_over_a_path(self):
        """A multi-source Application can carry both. The chart is the release,
        and its values block is the override that reaches the workload."""
        both = self.sourced(
            [
                "repoURL: https://charts.example.com\nchart: podinfo\ntargetRevision: 6.9.2\n",
                "repoURL: https://github.com/example/fleet\npath: overlays/prod-usc1/podinfo\n",
            ]
        )
        found = self.resolve(
            {
                "apps/podinfo.yaml": both,
                "overlays/prod-usc1/podinfo/kustomization.yaml": self.KUSTOMIZATION,
            },
            "prod-usc1",
            self.TRACKED,
        )
        self.assertEqual(found["renderer"], "helm")
        self.assertEqual(found["values_field"], "spec.sources[0].helm.valuesObject")

    def test_two_overlays_are_an_ambiguity_with_no_right_answer(self):
        two = self.sourced(
            [
                "repoURL: https://github.com/example/fleet\npath: overlays/prod-usc1/other\n",
                "repoURL: https://github.com/example/fleet\npath: overlays/prod-usc1/podinfo\n",
            ]
        )
        found = self.resolve(
            {
                "apps/podinfo.yaml": two,
                "overlays/prod-usc1/podinfo/kustomization.yaml": self.KUSTOMIZATION,
                "overlays/prod-usc1/other/kustomization.yaml": self.KUSTOMIZATION,
            },
            "prod-usc1",
            self.TRACKED,
        )
        self.assertIsNone(found)

    def test_a_path_escaping_the_clone_is_refused(self):
        """`..` would resolve a finding against whatever sits beside the clone
        on the agent's filesystem, so it never reads as an overlay."""
        escaping = self.OVERLAY.replace(
            "    path: overlays/prod-usc1/podinfo\n",
            "    path: ../../etc\n",
        )
        found = self.resolve(
            {
                "apps/podinfo.yaml": escaping,
                "overlays/prod-usc1/podinfo/kustomization.yaml": self.KUSTOMIZATION,
            },
            "prod-usc1",
            self.TRACKED,
        )
        self.assertIsNone(found)

    def test_an_overlay_gets_no_helm_release_key(self):
        """Kustomize output carries a tracking id and no `meta.helm.sh` pair,
        so a lookup by release coordinates must not find it."""
        found = self.resolve(
            {
                "apps/podinfo.yaml": self.OVERLAY,
                "overlays/prod-usc1/podinfo/kustomization.yaml": self.KUSTOMIZATION,
            },
            "prod-usc1",
            {"application": "", "name": "podinfo-overlay", "namespace": "podinfo"},
        )
        self.assertIsNone(found)


class TestCandidatesCarryTheirReleaseDeclaration(unittest.TestCase):
    """The release annotation has to survive the trip to a candidate too.

    Same joints as `TestCandidatesCarryTheirDeclaration`, same failure mode: a
    correct index that never reaches `emit` is indistinguishable from a fleet
    that installs no charts.
    """

    CLUSTER = {"name": "prod-usc1", "location": "us-central1", "status": "RUNNING"}
    APPLICATION = textwrap.dedent(
        """\
        apiVersion: argoproj.io/v1alpha1
        kind: Application
        metadata:
          name: charted
        spec:
          source:
            repoURL: https://charts.example.com
            chart: charted
            targetRevision: 1.2.3
          destination:
            name: prod-usc1
            namespace: default
        """
    )
    DECLARED_WORKLOAD = textwrap.dedent(
        """\
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: both
          namespace: default
        """
    )

    def clone(self, tmp):
        path = Path(tmp) / "infra"
        (path / "apps").mkdir(parents=True)
        (path / "apps/charted.yaml").write_text(self.APPLICATION)
        (path / "clusters/prod-usc1/workloads").mkdir(parents=True)
        (path / "clusters/prod-usc1/workloads/both.yaml").write_text(self.DECLARED_WORKLOAD)
        return path

    def fleet_run(self, *items):
        def run(argv, **kwargs):
            if argv[:4] == ["gcloud", "container", "clusters", "list"]:
                return Run(argv, 0, json.dumps([self.CLUSTER]), "", 0.01)
            # Compliance gates on the control-plane describe and reads node
            # pools; obtainability issues neither.
            if argv[:4] == ["gcloud", "container", "clusters", "describe"]:
                return Run(argv, 0, json.dumps({"privateClusterConfig": {}}), "", 0.01)
            if argv[:3] == ["gcloud", "container", "node-pools"]:
                return Run(argv, 0, "[]", "", 0.01)
            if argv[:2] == ["kubectl", "get"]:
                return Run(argv, 0, json.dumps(dump_of(*items)), "", 0.01)
            return Run(argv, 0, "", "", 0.01)

        return run

    def candidates(self, *items, audit_id="obtainability-audit"):
        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), \
                    patch.object(collect, "SCRATCH_DIR", tmp):
                manifest = collect.collect_fleet(
                    audit_id, "acme",
                    run=self.fleet_run(*items), workspace=self.clone(tmp),
                )
        return [c for cluster in manifest["clusters"] for c in cluster.get("candidates") or []]

    def tracked(self, name, **overrides):
        return deployment(
            name,
            **{
                "metadata.annotations": {
                    "argocd.argoproj.io/tracking-id": f"charted:apps/Deployment:default/{name}"
                },
                **overrides,
            },
        )

    def test_a_chart_rendered_workload_carries_its_release_declaration(self):
        candidates = self.candidates(self.tracked("rendered"))
        self.assertTrue(candidates)
        for candidate in candidates:
            self.assertEqual(
                candidate["release_declaration"],
                {
                    "path": "apps/charted.yaml",
                    "kind": "Application",
                    "renderer": "helm",
                    "chart": "charted",
                    "repo": "https://charts.example.com",
                    "version": "1.2.3",
                    "values_field": "spec.source.helm.valuesObject",
                },
                candidate["check"],
            )

    def test_a_chart_rendered_workload_carries_its_release_declaration_on_compliance_too(self):
        # Compliance builds its own workload records and cluster hits; both
        # used to drop the release, so §3's release branch never fired there.
        rendered = self.tracked(
            "rendered",
            **{"spec.template.spec.containers": [{"name": "app", "securityContext": {"privileged": True}}]},
        )
        candidates = self.candidates(rendered, audit_id="compliance-audit")
        on_the_workload = [c for c in candidates if c["object"] == "Deployment/rendered"]
        self.assertTrue(on_the_workload)
        for candidate in on_the_workload:
            self.assertEqual(candidate["release_declaration"]["path"], "apps/charted.yaml", candidate["check"])

    def test_an_unmanaged_workload_carries_neither_annotation(self):
        candidates = self.candidates(deployment("plain"))
        self.assertTrue(candidates)
        for candidate in candidates:
            self.assertNotIn("release_declaration", candidate, candidate["check"])
            self.assertNotIn("declaration", candidate, candidate["check"])

    def test_an_object_with_its_own_manifest_is_edited_there_instead(self):
        """A workload that is both declared and chart-tracked takes the direct
        path. Editing the object's own file is the stronger fix; a values
        override beside it would be a second expression of the same change,
        and only one of them can win the next sync.
        """
        candidates = self.candidates(self.tracked("both"))
        self.assertTrue(candidates)
        for candidate in candidates:
            self.assertIn("declaration", candidate, candidate["check"])
            self.assertNotIn("release_declaration", candidate, candidate["check"])


class TestNamespaceDirectories(unittest.TestCase):
    """Where a *new* object for a namespace goes, which neither index answers.

    The two above resolve an object that exists to the file declaring it. A
    finding whose fix is a NetworkPolicy or a ServiceAccount that does not
    exist yet cannot use either, and the model's own reasoning about it was
    wrong on 2026-09-07 in a way that read as careful -- see
    `collect.namespace_directories`.
    """

    OVERLAY_APPLICATION = textwrap.dedent(
        """\
        apiVersion: argoproj.io/v1alpha1
        kind: Application
        metadata:
          name: podinfo-kustomize
        spec:
          source:
            repoURL: https://github.com/acme/infra.git
            path: clusters/prod-usc1/kustomize/podinfo
          destination:
            name: prod-usc1
            namespace: podinfo-kustomize
        """
    )
    CHART_APPLICATION = textwrap.dedent(
        """\
        apiVersion: argoproj.io/v1alpha1
        kind: Application
        metadata:
          name: cert-manager
        spec:
          source:
            repoURL: https://charts.jetstack.io
            chart: cert-manager
            targetRevision: v1.14.4
          destination:
            name: prod-usc1
            namespace: cert-manager
        """
    )

    def declared(self, name, namespace):
        return textwrap.dedent(
            f"""\
            apiVersion: apps/v1
            kind: Deployment
            metadata:
              name: {name}
              namespace: {namespace}
            """
        )

    def resolve(self, files, *, directories=()):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for relative in directories:
                (root / relative).mkdir(parents=True, exist_ok=True)
            for relative, text in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            return collect.namespace_directories(
                collect.workload_declarations(root),
                collect.release_declarations(root),
                root,
            )

    def test_a_namespace_the_repo_already_declares_into_resolves_to_that_directory(self):
        found = self.resolve(
            {"clusters/prod-usc1/workloads/app.yaml": self.declared("app", "shop")}
        )
        self.assertEqual(
            found[("prod-usc1", "shop")],
            {"path": "clusters/prod-usc1/workloads", "source": "sibling"},
        )

    def test_a_kustomize_overlay_answers_for_the_namespace_it_renders_into(self):
        """The 2026-09-07 case. The Application's source is a Kustomize root in
        this repository, so a new object for the namespace goes into it -- the
        published finding said no file could carry the fix.
        """
        found = self.resolve(
            {
                "argocd/apps/podinfo-kustomize.yaml": self.OVERLAY_APPLICATION,
                "clusters/prod-usc1/kustomize/podinfo/kustomization.yaml": "resources: []\n",
            }
        )
        self.assertEqual(
            found[("prod-usc1", "podinfo-kustomize")],
            {
                "path": "clusters/prod-usc1/kustomize/podinfo",
                "source": "overlay",
                "declaration": "argocd/apps/podinfo-kustomize.yaml",
            },
        )

    def test_a_chart_answers_for_no_namespace(self):
        """A values override reaches an object the chart renders; it cannot
        create one. The chart's directory is in another repository.
        """
        found = self.resolve({"argocd/apps/cert-manager.yaml": self.CHART_APPLICATION})
        self.assertNotIn(("prod-usc1", "cert-manager"), found)

    def test_a_sibling_directory_outranks_an_overlay_over_the_same_namespace(self):
        """A plain file in a directory Argo CD applies needs no wiring at all,
        where a file in an overlay renders only once `resources:` names it.
        """
        found = self.resolve(
            {
                "argocd/apps/podinfo-kustomize.yaml": self.OVERLAY_APPLICATION,
                "clusters/prod-usc1/kustomize/podinfo/kustomization.yaml": "resources: []\n",
                "clusters/prod-usc1/workloads/app.yaml": self.declared(
                    "app", "podinfo-kustomize"
                ),
            }
        )
        self.assertEqual(found[("prod-usc1", "podinfo-kustomize")]["source"], "sibling")

    def test_two_directories_declaring_into_one_namespace_resolve_to_neither(self):
        found = self.resolve(
            {
                "clusters/prod-usc1/workloads/app.yaml": self.declared("app", "shop"),
                "clusters/prod-usc1/extra/other.yaml": self.declared("other", "shop"),
            }
        )
        self.assertNotIn(("prod-usc1", "shop"), found)

    def test_the_cluster_arm_answers_for_a_namespace_no_file_declares(self):
        """The arm with the yield. `cert-manager` is declared by nothing here,
        and a NetworkPolicy for it still belongs in the directory Argo CD
        applies to the cluster rather than in a fork of the chart.
        """
        found = self.resolve(
            {"clusters/prod-usc1/workloads/app.yaml": self.declared("app", "shop")}
        )
        self.assertEqual(
            found[("prod-usc1", collect.NAMESPACE_KEY_ANY)],
            {"path": "clusters/prod-usc1/workloads", "source": "cluster"},
        )

    def test_the_cluster_arm_needs_one_unambiguous_directory(self):
        found = self.resolve(
            {
                "clusters/prod-usc1/workloads/app.yaml": self.declared("app", "shop"),
                "clusters/prod-usc1/extra/other.yaml": self.declared("other", "wharf"),
            }
        )
        self.assertNotIn(("prod-usc1", collect.NAMESPACE_KEY_ANY), found)
        # Each namespace still resolves on its own: the ambiguity is about
        # which directory serves the *cluster*, not either namespace.
        self.assertEqual(found[("prod-usc1", "shop")]["source"], "sibling")
        self.assertEqual(found[("prod-usc1", "wharf")]["source"], "sibling")

    def overlaid(self, files):
        """`files`, plus the overlay Application and the kustomization proving
        `clusters/prod-usc1/kustomize/podinfo` is a Kustomize root.
        """
        return {
            "argocd/apps/podinfo-kustomize.yaml": self.OVERLAY_APPLICATION,
            "clusters/prod-usc1/kustomize/podinfo/kustomization.yaml": (
                "resources:\n  - deploy.yaml\n"
            ),
            **files,
        }

    def test_an_overlay_root_does_not_make_the_cluster_arm_ambiguous(self):
        """The live shape on 2026-09-07, and the reason this exclusion exists.

        `spot-capacity-test` had one plain-apply directory and one Kustomize
        root -- added by a merged `no-pdb` remediation -- so it was the one
        cluster in the clone whose fallback directory resolved to nothing.
        """
        found = self.resolve(
            self.overlaid(
                {
                    "clusters/prod-usc1/workloads/app.yaml": self.declared("app", "shop"),
                    "clusters/prod-usc1/kustomize/podinfo/deploy.yaml": self.declared(
                        "podinfo", "podinfo-kustomize"
                    ),
                }
            )
        )
        self.assertEqual(
            found[("prod-usc1", collect.NAMESPACE_KEY_ANY)],
            {"path": "clusters/prod-usc1/workloads", "source": "cluster"},
        )

    def test_a_manifest_below_an_overlay_root_is_excluded_with_it(self):
        """One level down inside an overlay is no more applied as it stands
        than the top of it, so it cannot be the directory the arm names either.
        """
        found = self.resolve(
            self.overlaid(
                {
                    "clusters/prod-usc1/workloads/app.yaml": self.declared("app", "shop"),
                    "clusters/prod-usc1/kustomize/podinfo/base/deploy.yaml": self.declared(
                        "podinfo", "podinfo-kustomize"
                    ),
                }
            )
        )
        self.assertEqual(
            found[("prod-usc1", collect.NAMESPACE_KEY_ANY)]["path"],
            "clusters/prod-usc1/workloads",
        )

    def test_a_cluster_whose_only_directory_is_an_overlay_root_gets_no_arm(self):
        """Excluding down to nothing is not the same as resolving. There is no
        directory here a file can simply be added to.
        """
        found = self.resolve(
            self.overlaid(
                {
                    "clusters/prod-usc1/kustomize/podinfo/deploy.yaml": self.declared(
                        "podinfo", "podinfo-kustomize"
                    )
                }
            )
        )
        self.assertNotIn(("prod-usc1", collect.NAMESPACE_KEY_ANY), found)

    def test_the_excluded_root_still_answers_for_its_own_namespace(self):
        """The exclusion is scoped to the cluster arm's count. The namespace
        the overlay renders into still resolves to it, which is what
        `audit_report.wire_kustomize_additions` then wires.
        """
        found = self.resolve(
            self.overlaid(
                {
                    "clusters/prod-usc1/workloads/app.yaml": self.declared("app", "shop"),
                    "clusters/prod-usc1/kustomize/podinfo/deploy.yaml": self.declared(
                        "podinfo", "podinfo-kustomize"
                    ),
                }
            )
        )
        self.assertEqual(
            found[("prod-usc1", "podinfo-kustomize")]["path"],
            "clusters/prod-usc1/kustomize/podinfo",
        )

    def test_a_second_plain_directory_is_still_ambiguous_alongside_an_overlay(self):
        """The exclusion must not over-fire. Two directories objects really are
        applied from is the ambiguity the arm has always refused.
        """
        found = self.resolve(
            self.overlaid(
                {
                    "clusters/prod-usc1/workloads/app.yaml": self.declared("app", "shop"),
                    "clusters/prod-usc1/extra/other.yaml": self.declared("other", "wharf"),
                    "clusters/prod-usc1/kustomize/podinfo/deploy.yaml": self.declared(
                        "podinfo", "podinfo-kustomize"
                    ),
                }
            )
        )
        self.assertNotIn(("prod-usc1", collect.NAMESPACE_KEY_ANY), found)

    def test_the_exclusion_is_scoped_to_the_cluster_the_application_targets(self):
        """A root is a root for the cluster its Application deploys to. Letting
        one cluster's overlay silence another's directory would resolve a
        second cluster to a tree Argo CD never applies there.
        """
        elsewhere = self.OVERLAY_APPLICATION.replace("name: prod-usc1", "name: prod-euw1")
        found = self.resolve(
            {
                "argocd/apps/podinfo-kustomize.yaml": elsewhere,
                "clusters/prod-usc1/kustomize/podinfo/kustomization.yaml": "resources: []\n",
                "clusters/prod-usc1/workloads/app.yaml": self.declared("app", "shop"),
                "clusters/prod-usc1/kustomize/podinfo/deploy.yaml": self.declared(
                    "podinfo", "podinfo-kustomize"
                ),
            }
        )
        self.assertNotIn(("prod-usc1", collect.NAMESPACE_KEY_ANY), found)

    def test_a_chart_release_is_not_an_excluded_root(self):
        """`_kustomize_roots` reads the renderer, not the presence of a path. A
        chart's directory is in another repository and excludes nothing here.
        """
        self.assertEqual(
            collect._kustomize_roots(
                {
                    ("prod-usc1", "application", "cert-manager"): {
                        "renderer": "helm",
                        "chart": "cert-manager",
                    },
                    ("prod-usc1", "application", "podinfo"): {
                        "renderer": "kustomize",
                        "chart": "clusters/prod-usc1/kustomize/podinfo",
                    },
                }
            ),
            {"prod-usc1": {"clusters/prod-usc1/kustomize/podinfo"}},
        )

    def test_a_namespace_restricting_appproject_withdraws_the_cluster_arm(self):
        found = self.resolve(
            {
                "clusters/prod-usc1/workloads/app.yaml": self.declared("app", "shop"),
                "argocd/projects/team.yaml": textwrap.dedent(
                    """\
                    apiVersion: argoproj.io/v1alpha1
                    kind: AppProject
                    metadata:
                      name: team
                    spec:
                      destinations:
                        - server: '*'
                          namespace: shop
                    """
                ),
            }
        )
        self.assertNotIn(("prod-usc1", collect.NAMESPACE_KEY_ANY), found)
        # The sibling arm is untouched: it names a directory that already
        # declares into that namespace, which the project plainly permits.
        self.assertEqual(found[("prod-usc1", "shop")]["source"], "sibling")

    def test_a_wildcard_appproject_leaves_the_cluster_arm_standing(self):
        found = self.resolve(
            {
                "clusters/prod-usc1/workloads/app.yaml": self.declared("app", "shop"),
                "argocd/projects/team.yaml": textwrap.dedent(
                    """\
                    apiVersion: argoproj.io/v1alpha1
                    kind: AppProject
                    metadata:
                      name: team
                    spec:
                      destinations:
                        - server: '*'
                          namespace: '*'
                    """
                ),
            }
        )
        self.assertEqual(found[("prod-usc1", collect.NAMESPACE_KEY_ANY)]["source"], "cluster")

    def test_an_appproject_with_no_destinations_withdraws_the_cluster_arm(self):
        """A project permitting nothing is a restriction, not the absence of
        one, and an unparseable file is treated the same way: cost findings
        rather than open wrong pull requests.
        """
        found = self.resolve(
            {
                "clusters/prod-usc1/workloads/app.yaml": self.declared("app", "shop"),
                "argocd/projects/team.yaml": textwrap.dedent(
                    """\
                    apiVersion: argoproj.io/v1alpha1
                    kind: AppProject
                    metadata:
                      name: team
                    spec: {}
                    """
                ),
            }
        )
        self.assertNotIn(("prod-usc1", collect.NAMESPACE_KEY_ANY), found)

    def test_an_empty_clone_resolves_nothing(self):
        self.assertEqual(self.resolve({}), {})

    def test_the_indexes_are_optional(self):
        """Absent PyYAML empties both, and this must return an empty map rather
        than raising -- the behaviour that shipped before it existed.
        """
        self.assertEqual(collect.namespace_directories({}, {}, None), {})


class TestCandidatesCarryTheirNamespaceDirectory(unittest.TestCase):
    """The annotation has to reach a candidate, as the other two do.

    Same failure mode as its two siblings above: a correct index that never
    reaches `emit` looks exactly like a repository with nowhere to put a file.
    """

    CLUSTER = {"name": "prod-usc1", "location": "us-central1", "status": "RUNNING"}

    def clone(self, tmp):
        path = Path(tmp) / "infra"
        (path / "clusters/prod-usc1/workloads").mkdir(parents=True)
        (path / "clusters/prod-usc1/workloads/app.yaml").write_text(
            textwrap.dedent(
                """\
                apiVersion: apps/v1
                kind: Deployment
                metadata:
                  name: app
                  namespace: shop
                """
            )
        )
        return path

    def candidates(self, *items):
        def run(argv, **kwargs):
            if argv[:4] == ["gcloud", "container", "clusters", "list"]:
                return Run(argv, 0, json.dumps([self.CLUSTER]), "", 0.01)
            if argv[:2] == ["kubectl", "get"]:
                return Run(argv, 0, json.dumps(dump_of(*items)), "", 0.01)
            return Run(argv, 0, "", "", 0.01)

        with TemporaryDirectory() as tmp:
            with patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), \
                    patch.object(collect, "SCRATCH_DIR", tmp):
                manifest = collect.collect_fleet(
                    "obtainability-audit", "acme", run=run, workspace=self.clone(tmp),
                )
        return [c for cluster in manifest["clusters"] for c in cluster.get("candidates") or []]

    def test_a_candidate_in_a_declared_namespace_carries_the_sibling_directory(self):
        candidates = self.candidates(deployment("app", **{"metadata.namespace": "shop"}))
        self.assertTrue(candidates)
        for candidate in candidates:
            self.assertEqual(
                candidate["namespace_directory"],
                {"path": "clusters/prod-usc1/workloads", "source": "sibling"},
                candidate["check"],
            )

    def test_a_candidate_elsewhere_on_the_cluster_falls_back_to_the_cluster_arm(self):
        """The namespace is declared by no file, which is the ordinary case and
        the one that produced 56 `manual` findings.
        """
        candidates = self.candidates(deployment("other", **{"metadata.namespace": "wharf"}))
        self.assertTrue(candidates)
        for candidate in candidates:
            self.assertEqual(
                candidate["namespace_directory"],
                {"path": "clusters/prod-usc1/workloads", "source": "cluster"},
                candidate["check"],
            )


class TestChecksRevision(unittest.TestCase):
    """Every collector tells the harness which version of itself ran.

    `audit_report.py` compares this run's revision with the previous run's to
    decide whether a finding that stopped appearing was fixed or merely stopped
    being looked for. A collector that publishes nothing gives it no signal,
    and the harness falls back to claiming a fix — which is what the cost audit
    did the morning `check_idle_namespace` lost its ResourceQuota arm.
    """

    MODULES = TestEvidenceCommandsArePasteable.MODULES

    def module(self, name):
        return sys.modules[name] if name in sys.modules else __import__(name)

    def test_every_collector_publishes_a_digest_of_its_own_source(self):
        for name in self.MODULES:
            with self.subTest(module=name):
                path = Path(__file__).resolve().parent / f"{name}.py"
                expected = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
                self.assertEqual(self.module(name).CHECKS_REVISION, expected)

    def test_the_revisions_are_distinct(self):
        """Each reads its own file. A shared or constant value says nothing.

        Three collectors publishing one digest would make every stream look
        like it moved whenever any one of them was edited.
        """
        revisions = {self.module(n).CHECKS_REVISION for n in self.MODULES}
        self.assertEqual(len(revisions), len(self.MODULES))

    def test_the_manifest_carries_it(self):
        """Driven, not grepped.

        The constant is inert unless it reaches the manifest `audit_report.py`
        reads. Asserting the assignment is present in the source passes just as
        well when the key lands in a branch nothing takes, and fails on a
        reformat that changes nothing — so each collector is actually run.
        Every `run` answers `[]`, so each lists an empty fleet and returns its
        manifest without reaching a cluster.
        """

        def run(argv, **kwargs):
            return Run(argv, 0, "[]", "", 0.01)

        for name in self.MODULES:
            with self.subTest(module=name):
                module = self.module(name)
                # `collect.py` serves three streams, so it alone is told which.
                args = ("obtainability-audit", "acme") if name == "collect" else ("acme",)
                manifest = module.collect_fleet(*args, run=run)
                self.assertEqual(manifest["checks_revision"], module.CHECKS_REVISION)
                self.assertEqual(manifest["version"], module.MANIFEST_VERSION)


class TestAdversarialReviewFixes(unittest.TestCase):
    """One test per defect the pre-PR adversarial pass confirmed against the
    collector; each names the failure it would have shipped."""

    def compliance(self, **kw):
        t = TestComplianceCollectCluster()
        return t.run_with(**kw)

    # §2.14: the ServiceAccount read was narrowed to `default`, so every named
    # account read as absent and the check could not fire on any cluster.
    def test_unbound_sa_automount_fires_end_to_end(self):
        pod = TestComplianceCollectCluster.benign_pod()
        pod["spec"]["serviceAccountName"] = "api-sa"
        ns = pod["metadata"]["namespace"]
        result = self.compliance(
            workload_items=[pod],
            sa_items=[default_sa(ns, automount=False), TestUnboundSaAutomount.sa("api-sa", ns=ns)],
        )
        self.assertIn("unbound-sa-automount", {c["check"] for c in result["candidates"]})
        sa_command = next(c for c in result["commands"] if c["check"] == "unbound-sa-automount")
        self.assertNotIn("--field-selector", sa_command["command"])

    def test_default_sa_automount_ignores_named_accounts(self):
        ctx = context_of(
            serviceaccounts=[TestUnboundSaAutomount.sa("api-sa", ns="default")],
            workloads=[{"kind": "Pod", "ns": "default", "name": "api", "spec": {}}],
        )
        self.assertEqual(collect.check_default_sa_automount(ctx), [])

    # A failed Config Connector read went to `checks_not_applicable`, which
    # leaves the coverage denominator: the run published complete and resolved
    # every open kcc-object-wedged finding on the hub.
    def test_an_undetermined_kcc_read_is_unevaluated_not_inapplicable(self):
        result = self.compliance(kcc_items=lambda argv, **kw: Run(argv, collect.TIMEOUT_RC, "", "", 60.0))
        na = {e["check"] for e in result.get("checks_not_applicable") or []}
        self.assertNotIn("kcc-object-wedged", na)
        self.assertNotIn("kcc-object-wedged", {c["check"] for c in result["commands"]})
        unevaluated = {e["check"]: e["reason"] for e in result["checks_unevaluated"]}
        self.assertIn("Undetermined", unevaluated["kcc-object-wedged"])

    def test_an_absent_kcc_type_is_still_inapplicable(self):
        result = self.compliance()
        self.assertIn("kcc-object-wedged", {e["check"] for e in result["checks_not_applicable"]})
        self.assertNotIn("checks_unevaluated", result)

    # Any ccnp failure read as "no cluster-wide policies".
    def test_a_forbidden_ccnp_read_leaves_netpol_missing_unevaluated(self):
        forbidden = Run(["kubectl"], 1, "", "Error from server (Forbidden): ccnp is forbidden", 0.05)
        result = self.compliance(netpol_items=[namespace("default")], ccnp_run=forbidden)
        self.assertNotIn("netpol-missing", {c["check"] for c in result["candidates"]})
        self.assertNotIn("netpol-missing", {c["check"] for c in result["commands"]})
        self.assertIn("netpol-missing", {e["check"] for e in result["checks_unevaluated"]})

    # §3.2's excerpt published the whole token, which for `python3 -c` is the
    # program -- secrets written inline included.
    def test_trust_remote_code_excerpt_carries_only_the_setting(self):
        program = "from x import load; load(token='hf_SECRETSECRET', trust_remote_code=True)"
        w = ai_workload(container={"command": ["python3", "-c", program]})
        w = collect.normalize_ai_workloads(dump_of(w))[0]
        hit = collect.check_model_remote_code_trusted(w, {})
        self.assertIsNotNone(hit)
        self.assertNotIn("hf_SECRET", hit["excerpt"])
        self.assertIn("trust_remote_code", hit["excerpt"])

    # The vendor-CRD exception suppressed Kubernetes' own groups too, where a
    # `*` over `rbac.authorization.k8s.io` is self-granted cluster-admin.
    def test_a_wildcard_over_a_builtin_group_is_flagged(self):
        for group in ("rbac.authorization.k8s.io", "apps"):
            with self.subTest(group=group):
                rule = [{"verbs": ["*"], "resources": ["*"], "apiGroups": [group]}]
                ctx = context_of(
                    roles=[cluster_role("r", rule)],
                    clusterrolebindings=[role_binding("ClusterRole", "r", [subject("ServiceAccount", "app", "default")])],
                )
                self.assertEqual(len(collect.check_wildcard_rbac(ctx)), 1)

    def test_a_sig_project_crd_group_stays_a_vendor_group(self):
        rule = [{"verbs": ["*"], "resources": ["*"], "apiGroups": ["gateway.networking.x-k8s.io"]}]
        ctx = context_of(
            roles=[cluster_role("r", rule)],
            clusterrolebindings=[role_binding("ClusterRole", "r", [subject("ServiceAccount", "app", "default")])],
        )
        self.assertEqual(collect.check_wildcard_rbac(ctx), [])

    # Owned HPAs are skipped only where their min/max is graded.
    def test_an_owned_hpa_with_one_min_is_not_graded_by_3_6(self):
        ctx = context_of(hpas={"default": [hpa("h", min_replicas=3, max_replicas=3, owned=True)]})
        self.assertEqual(collect.check_hpa_cannot_scale(ctx), [])

    # policy/v1: a null selector selects no pods.
    def test_a_selectorless_pdb_protects_and_blocks_nothing(self):
        bare = pdb("p", max_unavailable=0)
        del bare["spec"]["selector"]
        ctx = TestBlockingPdb().ctx(bare)
        self.assertEqual(collect.check_blocking_pdb(ctx), [])
        self.assertIsNotNone(collect.check_no_pdb(ctx["workloads"][0], ctx))

    # §3.4's Do-NOT-flag for local and object-store paths.
    def test_a_local_or_object_store_model_needs_no_revision(self):
        for value in ("/models/llama", "./m", "gs://bucket/llama", "s3://b/m", "file:///m"):
            with self.subTest(value=value):
                self.assertIsNone(TestModelArtifactUnpinnedSource().hit({"args": ["--model", value]}))
                self.assertIsNone(TestModelArtifactUnpinnedSource().hit({"args": [f"--model={value}"]}))
        self.assertIsNotNone(TestModelArtifactUnpinnedSource().hit({"args": ["--model", "meta-llama/Llama-3"]}))

    # A `/` in the userinfo password stopped the redaction early.
    def test_a_password_with_a_slash_is_still_redacted(self):
        self.assertEqual(collect._ai_safe_url("https://u:pa/ss@host/m"), "https://host/m")

    # §2.3's read-only log-shipper mounts are minor.
    def test_a_readonly_log_mount_is_minor(self):
        for path in ("/var/log", "/var/lib/docker/containers"):
            with self.subTest(path=path):
                hit = collect.check_hostpath_mount(TestHostpathMount().wl(path, True), context_of())
                self.assertEqual(hit["severity"], "minor")
        self.assertEqual(collect.check_hostpath_mount(TestHostpathMount().wl("/var/log", False), context_of())["severity"], "critical")

    # `NotIn` excludes a node; it does not pin to one.
    def test_a_notin_hostname_affinity_is_not_rigid(self):
        affinity = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
            {"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "NotIn", "values": ["node-1"]}]}
        ]}}}
        self.assertIsNone(collect.check_rigid_scheduling(TestRigidScheduling().wl(affinity=affinity), context_of()))

    def test_lb_world_open_names_the_ranges_it_found(self):
        hits = collect.check_lb_world_open({"services": [lb_service(source_ranges=["0.0.0.0/0"])]})
        self.assertEqual(len(hits), 1)
        self.assertNotIn("no loadBalancerSourceRanges", hits[0]["excerpt"])
        self.assertIn("0.0.0.0/0", hits[0]["excerpt"])


class TestSecondReviewFixes(unittest.TestCase):
    def test_a_job_between_retries_has_not_finished(self):
        retrying = job("j1", failed=1)
        retrying["status"].pop("conditions")
        self.assertFalse(collect._job_finished(retrying))
        self.assertTrue(collect._job_finished(job("j2", failed=1)))
        self.assertTrue(collect._job_finished(job("j3", succeeded=1)))

    def test_trust_remote_code_opt_outs_are_not_findings(self):
        for args in (
            ["--no-trust-remote-code"],
            ["--trust-remote-code", "false"],
            ["--trust_remote_code=False"],
            ["--hf-overrides", '{"trust_remote_code": false}'],
        ):
            self.assertIsNone(collect._container_trusts_remote_code({"name": "c", "args": args}), args)
        for args in (["--trust-remote-code"], ["--trust-remote-code", "--port", "80"], ["--trust-remote-code=true"]):
            self.assertIsNotNone(collect._container_trusts_remote_code({"name": "c", "args": args}), args)

    def test_model_urls_are_published_without_the_token_around_them(self):
        urls = collect._model_artifact_urls(
            [], [{"name": "DOWNLOAD_OPTS", "value": "--api-key=sk-live-abc123 --src=http://mirror/models/m.safetensors"}]
        )
        self.assertEqual(urls, ["http://mirror/models/m.safetensors"])

    def test_a_service_endpoint_url_is_not_a_model_artifact(self):
        env = [
            {"name": "OPENAI_API_BASE", "value": "http://vllm.ml.svc:8000/v1"},
            {"name": "OTEL_EXPORTER_OTLP_ENDPOINT", "value": "http://otel-collector:4317"},
        ]
        self.assertEqual(collect._model_artifact_urls(["--port=8000", "http://metrics:9090/push"], env), [])

    def test_a_model_flag_or_variable_makes_a_url_a_model_artifact(self):
        self.assertEqual(collect._model_artifact_urls(["--model", "http://mirror/llama"], []), ["http://mirror/llama"])
        self.assertEqual(
            collect._model_artifact_urls([], [{"name": "MODEL_URL", "value": "http://mirror/llama"}]),
            ["http://mirror/llama"],
        )

    def test_a_role_binding_marks_only_its_own_namespaces_role_bound(self):
        role_ref = {"kind": "Role", "name": "manager"}
        self.assertNotEqual(
            collect._role_ref_key(role_ref, "team-b"),
            collect._role_ref_key({"kind": "Role"}, "team-a", name="manager"),
        )
        cluster_ref = {"kind": "ClusterRole", "name": "admin"}
        self.assertEqual(
            collect._role_ref_key(cluster_ref, "team-b"),
            collect._role_ref_key({"kind": "ClusterRole"}, "", name="admin"),
        )


class TestDefaultRun(unittest.TestCase):
    def test_a_timed_out_child_leaves_its_output_as_text(self):
        # `TimeoutExpired` carries the partial output as bytes whatever `text=`
        # said, and every reader of `Run` treats it as str.
        result = collect.default_run([sys.executable, "-c", "import sys, time; print(1, flush=True); print(2, file=sys.stderr, flush=True); time.sleep(5)"], timeout=1)
        self.assertEqual(result.rc, collect.TIMEOUT_RC)
        self.assertIsInstance(result.stdout, str)
        self.assertIsInstance(result.stderr, str)


if __name__ == "__main__":
    unittest.main()
