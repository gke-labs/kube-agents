#!/usr/bin/env python3
"""Unit tests for stall_report.py: fixture objects, kubectl stubbed."""

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import stall_report  # noqa: E402

NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)
NAMESPACE = "payments"


def stamp(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def managed(minutes_ago: float, field: str = "f:spec", operation: str = "Update") -> dict:
    return {"operation": operation, "time": stamp(minutes_ago), "fieldsV1": {field: {}}}


def obj(kind: str, name: str, spec=None, status=None, created_minutes_ago=120, managed_fields=None):
    meta = {
        "name": name,
        "namespace": NAMESPACE,
        "generation": 1,
        "creationTimestamp": stamp(created_minutes_ago),
    }
    if managed_fields is not None:
        meta["managedFields"] = managed_fields
    out = {"kind": kind, "metadata": meta}
    if spec is not None:
        out["spec"] = spec
    if status is not None:
        out["status"] = status
    return out


def condition(ctype: str, status: str, minutes_ago: float, reason: str = "") -> dict:
    return {"type": ctype, "status": status, "lastTransitionTime": stamp(minutes_ago), "reason": reason}


def warning(kind: str, name: str, count: int, first_ago: float, last_ago: float, reason="SYNC") -> dict:
    return {
        "type": "Warning",
        "reason": reason,
        "message": "error  processing   listener",
        "count": count,
        "firstTimestamp": stamp(first_ago),
        "lastTimestamp": stamp(last_ago),
        "involvedObject": {"kind": kind, "name": name, "namespace": NAMESPACE},
    }


class FakeResolver(stall_report.NameResolver):
    """A resolver over an in-memory namespace: {(resource, namespace): names}."""

    def __init__(self, existing: dict):
        super().__init__(lister=lambda resource, ns: existing.get((resource, ns)))
        self.existing = existing


class FakeReader:
    """Answers the scaled-to-zero reads from {args tuple: listing}; anything
    else reads as an empty list. `calls` records every read."""

    def __init__(self, listings=None):
        self.listings = listings or {}
        self.calls = []

    def __call__(self, args):
        self.calls.append(tuple(args))
        listing = self.listings.get(tuple(args), {"items": []})
        if isinstance(listing, Exception):
            raise listing
        return listing


def analyze(objects, events=(), existing=None, override=None, listings=None):
    resolver = FakeResolver(existing or {})
    scale = stall_report.ScaleResolver(reader=FakeReader(listings))
    return stall_report.analyze(list(objects), list(events), resolver, NOW, override, scale)


def healthy_deployment(name="web") -> dict:
    return obj(
        "Deployment",
        name,
        spec={"replicas": 2, "template": {"spec": {"containers": [{"name": "app", "image": "x"}]}}},
        status={
            "observedGeneration": 1,
            "conditions": [
                condition("Available", "True", 300, "MinimumReplicasAvailable"),
                condition("Progressing", "True", 300, "NewReplicaSetAvailable"),
            ],
        },
        managed_fields=[managed(300), managed(299, "f:status")],
    )


class GenerationLag(unittest.TestCase):
    def test_lag_past_threshold_is_reported(self):
        cr = obj("CertificateRequest", "api-cert", spec={}, status={"observedGeneration": 2})
        cr["metadata"]["generation"] = 3
        cr["metadata"]["managedFields"] = [managed(40), managed(1, "f:status")]
        rows = analyze([cr])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["heuristic"], "generation-lag")
        self.assertEqual(rows[0]["object"], "CertificateRequest/api-cert")
        self.assertIn("generation 3, observedGeneration 2", rows[0]["detail"])
        self.assertEqual(rows[0]["stalled_for"], "40m")

    def test_lag_under_threshold_is_not_reported(self):
        cr = obj("CertificateRequest", "api-cert", spec={}, status={"observedGeneration": 2})
        cr["metadata"]["generation"] = 3
        cr["metadata"]["managedFields"] = [managed(5)]
        self.assertEqual(analyze([cr]), [])

    def test_status_write_does_not_reset_the_age(self):
        cr = obj("Thing", "t", spec={}, status={"observedGeneration": 1}, managed_fields=[managed(50), managed(1, "f:status")])
        cr["metadata"]["generation"] = 2
        rows = analyze([cr])
        self.assertEqual(rows[0]["stalled_for"], "50m")

    def test_no_observed_generation_is_skipped(self):
        svc = obj("Service", "svc", spec={"ports": []}, status={"loadBalancer": {}})
        svc["metadata"]["generation"] = 4
        self.assertEqual(analyze([svc]), [])

    def test_deployment_uses_its_shorter_horizon(self):
        dep = healthy_deployment()
        dep["metadata"]["generation"] = 2
        dep["metadata"]["managedFields"] = [managed(12)]
        rows = analyze([dep])
        self.assertEqual([r["heuristic"] for r in rows], ["generation-lag"])
        self.assertEqual(analyze([dep], override=15), [])


class StaleConditions(unittest.TestCase):
    def test_stale_false_condition_is_reported(self):
        dep = healthy_deployment()
        dep["status"]["conditions"] = [
            condition("Available", "False", 30, "MinimumReplicasUnavailable"),
            condition("Progressing", "False", 30, "ProgressDeadlineExceeded"),
        ]
        rows = analyze([dep])
        self.assertEqual({r["heuristic"] for r in rows}, {"stale-condition"})
        self.assertEqual(len(rows), 2)
        self.assertIn("Progressing=False ProgressDeadlineExceeded", [r["detail"] for r in rows])

    def test_fresh_false_condition_is_not_reported(self):
        dep = healthy_deployment()
        dep["status"]["conditions"] = [condition("Progressing", "True", 2, "ReplicaSetUpdated"), condition("Available", "False", 2)]
        self.assertEqual(analyze([dep]), [])

    def test_config_connector_in_progress_states_are_not_stalls(self):
        for reason in ("Updating", "Deleting"):
            with self.subTest(reason=reason):
                sql = obj("SQLInstance", "orders-db", spec={}, status={"observedGeneration": 1, "conditions": [condition("Ready", "False", 40, reason)]})
                self.assertEqual(analyze([sql]), [])
                # A create in progress lags its generation too; not a row either.
                sql["metadata"]["generation"] = 2
                sql["metadata"]["managedFields"] = [managed(40)]
                self.assertEqual(analyze([sql]), [])
        failed = obj("SQLInstance", "orders-db", status={"conditions": [condition("Ready", "False", 20, "UpdateFailed")]})
        rows = analyze([failed])
        self.assertEqual([r["detail"] for r in rows], ["Ready=False UpdateFailed"])

    def test_dependency_not_ready_waits_an_hour(self):
        pool = obj("ContainerNodePool", "pool-a", status={"conditions": [condition("Ready", "False", 40, "DependencyNotReady")]})
        self.assertEqual(analyze([pool]), [])
        pool = obj("ContainerNodePool", "pool-a", status={"conditions": [condition("Ready", "False", 70, "DependencyNotReady")]})
        rows = analyze([pool])
        self.assertEqual([r["detail"] for r in rows], ["Ready=False DependencyNotReady"])
        self.assertEqual(rows[0]["stalled_for"], "1h10m")
        # A run-wide threshold above the hour applies to it as to every kind.
        pool = obj("ContainerNodePool", "pool-a", status={"conditions": [condition("Ready", "False", 70, "DependencyNotReady")]})
        self.assertEqual(analyze([pool], override=120), [])
        # Its generation lag waits the same hour, so a spec edit while it
        # waits on its parent is not a row sooner than the condition.
        pool = obj("ContainerNodePool", "pool-a", spec={}, status={"observedGeneration": 1, "conditions": [condition("Ready", "False", 40, "DependencyNotReady")]})
        pool["metadata"]["generation"] = 2
        pool["metadata"]["managedFields"] = [managed(40)]
        self.assertEqual(analyze([pool]), [])
        pool["metadata"]["managedFields"] = [managed(70)]
        pool["status"]["conditions"] = [condition("Ready", "False", 70, "DependencyNotReady")]
        self.assertEqual(sorted(r["heuristic"] for r in analyze([pool])), ["generation-lag", "stale-condition"])

    def test_unrelated_condition_types_are_ignored(self):
        hpa = obj("HorizontalPodAutoscaler", "web", status={"conditions": [condition("ScalingLimited", "False", 500)]})
        self.assertEqual(analyze([hpa]), [])

    def test_nested_listener_condition_names_the_listener(self):
        gw = obj(
            "Gateway",
            "edge",
            spec={"listeners": [{"name": "https", "tls": {"certificateRefs": [{"name": "edge-tls"}]}}]},
            status={
                "conditions": [condition("Accepted", "True", 100), condition("Programmed", "True", 100)],
                "listeners": [
                    {
                        "name": "https",
                        "conditions": [condition("ResolvedRefs", "False", 90, "InvalidCertificateRef")],
                    }
                ],
            },
            managed_fields=[managed(100)],
        )
        rows = analyze([gw], existing={("secrets", NAMESPACE): {"other"}})
        details = {r["heuristic"]: r["detail"] for r in rows}
        self.assertEqual(details["stale-condition"], "listeners[https] ResolvedRefs=False InvalidCertificateRef")

    def test_epoch_transition_time_is_clamped_to_creation(self):
        gw = obj("Gateway", "edge", spec={}, status={"conditions": [
            {"type": "Accepted", "status": "Unknown", "reason": "Pending", "lastTransitionTime": "1970-01-01T00:00:00Z"},
        ]}, created_minutes_ago=5)
        self.assertEqual(analyze([gw]), [])
        gw["metadata"]["creationTimestamp"] = stamp(40)
        rows = analyze([gw])
        self.assertEqual([r["stalled_for"] for r in rows], ["40m"])

    def test_finished_pod_is_skipped_by_every_heuristic(self):
        for phase in ("Succeeded", "Failed"):
            pod = obj(
                "Pod",
                "job-x",
                spec={"volumes": [{"name": "cfg", "configMap": {"name": "gone"}}]},
                status={"phase": phase, "observedGeneration": 0, "conditions": [condition("Ready", "False", 900, "PodCompleted")]},
                managed_fields=[managed(900)],
            )
            pod["metadata"]["generation"] = 1
            events = [warning("Pod", "job-x", 9, first_ago=800, last_ago=1)]
            self.assertEqual(analyze([pod], events=events, existing={("configmaps", NAMESPACE): set()}), [], phase)

    def test_retired_replicaset_is_skipped_by_every_heuristic(self):
        rs = obj(
            "ReplicaSet",
            "web-old",
            spec={"replicas": 0, "template": {"spec": {"containers": [{"name": "app", "envFrom": [{"configMapRef": {"name": "web-config-abc123"}}]}]}}},
            status={"observedGeneration": 1, "replicas": 0, "conditions": [condition("Ready", "False", 900)]},
            managed_fields=[managed(900)],
        )
        rs["metadata"]["generation"] = 2
        events = [warning("ReplicaSet", "web-old", 9, first_ago=800, last_ago=1)]
        existing = {("configmaps", NAMESPACE): set()}
        self.assertEqual(analyze([rs], events=events, existing=existing), [])
        rs["spec"]["replicas"] = 1
        rows = analyze([rs], events=events, existing=existing)
        self.assertIn("dangling-reference", [r["heuristic"] for r in rows])

    def test_finished_job_is_skipped_by_every_heuristic(self):
        def job(conditions):
            j = obj(
                "Job",
                "backup-1",
                spec={"template": {"spec": {"containers": [{"name": "app", "envFrom": [{"secretRef": {"name": "backup-creds"}}]}]}}},
                status={"observedGeneration": 0, "conditions": conditions},
                managed_fields=[managed(900)],
            )
            j["metadata"]["generation"] = 1
            return j

        events = [warning("Job", "backup-1", 9, first_ago=800, last_ago=1)]
        existing = {("secrets", NAMESPACE): set()}
        for ctype in ("Complete", "Failed"):
            finished = job([condition("Ready", "False", 900), condition(ctype, "True", 850)])
            self.assertEqual(analyze([finished], events=events, existing=existing), [], ctype)
        running = job([condition("Complete", "False", 850)])
        rows = analyze([running], events=events, existing=existing)
        self.assertIn("dangling-reference", [r["heuristic"] for r in rows])

    def test_paused_deployment_is_not_a_stall(self):
        dep = healthy_deployment()
        dep["spec"]["paused"] = True
        dep["status"]["conditions"] = [condition("Progressing", "Unknown", 600, "DeploymentPaused")]
        self.assertEqual(analyze([dep]), [])

    def test_route_parent_condition_names_the_gateway(self):
        route = obj(
            "HTTPRoute",
            "web",
            spec={},
            status={"parents": [
                {"parentRef": {"name": "edge"}, "conditions": [condition("Accepted", "True", 100)]},
                {"parentRef": {"name": "shared", "namespace": "infra"}, "conditions": [condition("Accepted", "False", 100, "NotAllowedByListeners")]},
            ]},
        )
        rows = analyze([route])
        self.assertEqual([r["detail"] for r in rows], ["parents[shared] Accepted=False NotAllowedByListeners"])


def workload(kind, name, replicas, labels, namespace=NAMESPACE):
    return {"kind": kind, "metadata": {"name": name, "namespace": namespace, "labels": labels},
            "spec": {"replicas": replicas, "template": {"metadata": {"labels": labels}}}}


def off_gateway(name="edge", class_name="eg", listener_programmed="True"):
    return obj(
        "Gateway",
        name,
        spec={"gatewayClassName": class_name, "listeners": [{"name": "http"}]},
        status={
            "conditions": [condition("Accepted", "True", 600), condition("Programmed", "False", 600, "NoResources")],
            "listeners": [{"name": "http", "attachedRoutes": 1, "conditions": [condition("Programmed", listener_programmed, 600, "Invalid")]}],
        },
        managed_fields=[managed(600)],
    )


ENVOY = "gateway.envoyproxy.io/gatewayclass-controller"
ENVOY_SELECTOR = f"gateway.envoyproxy.io/owning-gateway-name=edge,gateway.envoyproxy.io/owning-gateway-namespace={NAMESPACE}"


def gateway_class(name, controller):
    return {("get", "gatewayclasses.gateway.networking.k8s.io", name): {"kind": "GatewayClass", "metadata": {"name": name}, "spec": {"controllerName": controller}}}


def envoy_proxy(replicas, namespace="envoy-gateway-system"):
    """Envoy Gateway's proxy for Gateway edge, in its controller namespace;
    the Gateway's own namespace holds none."""
    return {
        ("get", "deployments", "-l", ENVOY_SELECTOR, "-n", namespace): {"items": [workload("Deployment", f"envoy-{NAMESPACE}-edge-abc", replicas, {}, namespace=namespace)]},
    }


class ScaledToZero(unittest.TestCase):
    """A Gateway whose in-cluster proxy is scaled to zero on purpose."""

    def test_the_programmed_row_goes_when_the_envoy_proxy_is_at_zero(self):
        listings = {**gateway_class("eg", ENVOY), **envoy_proxy(0)}
        self.assertEqual(analyze([off_gateway()], listings=listings), [])

    def test_the_programmed_row_stays_when_the_envoy_proxy_runs(self):
        listings = {**gateway_class("eg", ENVOY), **envoy_proxy(1)}
        rows = analyze([off_gateway()], listings=listings)
        self.assertEqual([r["detail"] for r in rows], ["Programmed=False NoResources"])

    def test_every_other_row_of_the_gateway_stays(self):
        # A listener that is not programmed, and a missing certificate, are
        # not what a scale to zero causes.
        gateway = off_gateway(listener_programmed="False")
        gateway["spec"]["listeners"][0]["tls"] = {"certificateRefs": [{"name": "edge-tls"}]}
        listings = {**gateway_class("eg", ENVOY), **envoy_proxy(0)}
        rows = analyze([gateway], existing={("secrets", NAMESPACE): set()}, listings=listings)
        self.assertEqual(
            sorted(r["detail"] for r in rows),
            ["listeners[0].tls.certificateRefs -> Secret/edge-tls not found", "listeners[http] Programmed=False Invalid"],
        )

    def test_another_controllers_proxy_is_found_by_the_standard_label_in_the_gateways_namespace(self):
        listings = {
            **gateway_class("istio", "istio.io/gateway-controller"),
            ("get", "deployments", "-l", "gateway.networking.k8s.io/gateway-name=edge", "-n", NAMESPACE): {"items": [workload("Deployment", "edge-istio", 0, {})]},
        }
        self.assertEqual(analyze([off_gateway(class_name="istio")], listings=listings), [])

    def test_a_zero_replica_deployment_in_another_namespace_does_not_silence_the_gateway(self):
        # Only the Gateway's namespace and Envoy Gateway's controller
        # namespace are read; a look-alike planted elsewhere is never seen.
        reader = FakeReader({**gateway_class("eg", ENVOY), **envoy_proxy(0, namespace="tenant-b")})
        rows = stall_report.analyze([off_gateway()], [], FakeResolver({}), NOW, None, stall_report.ScaleResolver(reader=reader))
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            sorted(c for c in reader.calls if c[1] == "deployments"),
            [("get", "deployments", "-l", ENVOY_SELECTOR, "-n", "envoy-gateway-system"), ("get", "deployments", "-l", ENVOY_SELECTOR, "-n", NAMESPACE)],
        )

    def test_a_gateway_with_no_in_cluster_proxy_keeps_the_row(self):
        # GKE's managed classes run no proxy Deployment, so none is found.
        listings = gateway_class("gke-l7-rilb", "networking.gke.io/gateway")
        self.assertEqual(len(analyze([off_gateway(class_name="gke-l7-rilb")], listings=listings)), 1)

    def test_a_failed_read_keeps_the_row(self):
        listings = {**gateway_class("eg", ENVOY), ("get", "deployments", "-l", ENVOY_SELECTOR, "-n", NAMESPACE): RuntimeError("forbidden"), **envoy_proxy(0)}
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(len(analyze([off_gateway()], listings=listings)), 1)
        self.assertIn("scaled-to-zero check could not read", err.getvalue())

    def test_a_route_is_left_alone(self):
        route = obj(
            "HTTPRoute",
            "web",
            spec={"parentRefs": [{"name": "edge", "sectionName": "htps"}], "rules": [{"backendRefs": [{"name": "web", "port": 80}]}]},
            status={"parents": [{"parentRef": {"name": "edge"}, "conditions": [condition("Accepted", "False", 600, "NoMatchingParent")]}]},
        )
        reader = FakeReader({**gateway_class("eg", ENVOY), **envoy_proxy(0)})
        rows = stall_report.analyze([route], [], FakeResolver({}), NOW, None, stall_report.ScaleResolver(reader=reader))
        self.assertEqual([r["detail"] for r in rows], ["parents[edge] Accepted=False NoMatchingParent"])
        self.assertEqual(reader.calls, [])

    def test_a_healthy_gateway_costs_no_read(self):
        gateway = off_gateway()
        gateway["status"]["conditions"][1] = condition("Programmed", "True", 600)
        reader = FakeReader()
        rows = stall_report.analyze([gateway, healthy_deployment()], [], FakeResolver({}), NOW, None, stall_report.ScaleResolver(reader=reader))
        self.assertEqual((rows, reader.calls), ([], []))

    def test_a_workload_scaled_to_zero_leaves_no_scheduling_rows(self):
        # Its Pods are deleted, so their FailedScheduling events name no
        # object in the scan and are not reported.
        dep = healthy_deployment()
        dep["spec"]["replicas"] = 0
        events = [warning("Pod", "web-abc-123", 9, first_ago=60, last_ago=1, reason="FailedScheduling")]
        self.assertEqual(analyze([dep], events=events), [])


class RepeatingWarnings(unittest.TestCase):
    def test_rising_count_over_the_threshold_is_reported(self):
        gw = obj("Gateway", "edge", spec={}, status={})
        rows = analyze([gw], events=[warning("Gateway", "edge", 27, first_ago=40, last_ago=1)])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["heuristic"], "repeating-warnings")
        self.assertEqual(rows[0]["detail"], "SYNC x27: error processing listener")
        self.assertEqual(rows[0]["stalled_for"], "39m")

    def test_single_or_short_or_stale_warnings_are_not_reported(self):
        gw = obj("Gateway", "edge", spec={}, status={})
        once = warning("Gateway", "edge", 1, first_ago=40, last_ago=40)
        brief = warning("Gateway", "edge", 5, first_ago=5, last_ago=1)
        long_ago = warning("Gateway", "edge", 30, first_ago=400, last_ago=200)
        normal = dict(warning("Gateway", "edge", 30, first_ago=40, last_ago=1), type="Normal")
        self.assertEqual(analyze([gw], events=[once, brief, long_ago, normal]), [])

    def test_events_on_other_objects_do_not_attach(self):
        gw = obj("Gateway", "edge", spec={}, status={})
        self.assertEqual(analyze([gw], events=[warning("Gateway", "other", 27, 40, 1)]), [])

    def test_long_sync_message_keeps_the_secret_name(self):
        # The GKE Gateway controller's SYNC event names the missing Secret last,
        # and on the default read-only identity this row is what names it.
        gw = obj("Gateway", "checkout-gateway", spec={}, status={})
        event = warning("Gateway", "checkout-gateway", 4, first_ago=40, last_ago=1)
        event["message"] = (
            'failed to translate Gateway "seeded-reliability/checkout-gateway": '
            "Error GWCER102: Secret seeded-reliability/checkout-tls not found."
        )
        rows = analyze([gw], events=[event])
        self.assertEqual(len(rows), 1)
        self.assertGreater(len(rows[0]["detail"]), 100)
        self.assertTrue(rows[0]["detail"].endswith("Secret seeded-reliability/checkout-tls not found."))
        self.assertIn("Secret seeded-reliability/checkout-tls not found.", stall_report.render_table(rows))

    def test_events_v1_series_shape_is_read(self):
        gw = obj("Gateway", "edge", spec={}, status={})
        event = {
            "type": "Warning",
            "reason": "SYNC",
            "message": "m",
            "eventTime": stamp(40),
            "series": {"count": 12, "lastObservedTime": stamp(2)},
            "involvedObject": {"kind": "Gateway", "name": "edge"},
        }
        rows = analyze([gw], events=[event])
        self.assertEqual(len(rows), 1)
        self.assertIn("x12", rows[0]["detail"])


class DanglingReferences(unittest.TestCase):
    def test_gateway_listener_naming_an_absent_secret(self):
        gw = obj(
            "Gateway",
            "edge",
            spec={"gatewayClassName": "gke-l7-regional-external-managed", "listeners": [{"name": "https", "tls": {"certificateRefs": [{"kind": "Secret", "name": "edge-tls"}]}}]},
            status={},
            managed_fields=[managed(45)],
        )
        rows = analyze([gw], existing={("secrets", NAMESPACE): set()})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["heuristic"], "dangling-reference")
        self.assertEqual(rows[0]["detail"], "listeners[0].tls.certificateRefs -> Secret/edge-tls not found")
        self.assertEqual(rows[0]["stalled_for"], "45m")

    def test_deployment_envfrom_naming_an_absent_configmap(self):
        dep = healthy_deployment()
        dep["spec"]["template"]["spec"]["containers"][0]["envFrom"] = [{"configMapRef": {"name": "web-config"}}]
        dep["metadata"]["managedFields"] = [managed(30)]
        rows = analyze([dep], existing={("configmaps", NAMESPACE): {"something-else"}})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["heuristic"], "dangling-reference")
        self.assertIn("ConfigMap/web-config not found", rows[0]["detail"])

    def test_present_and_optional_references_are_quiet(self):
        dep = healthy_deployment()
        container = dep["spec"]["template"]["spec"]["containers"][0]
        container["envFrom"] = [{"configMapRef": {"name": "web-config"}}, {"secretRef": {"name": "absent", "optional": True}}]
        dep["spec"]["template"]["spec"]["volumes"] = [{"name": "v", "configMap": {"name": "web-config"}}]
        rows = analyze([dep], existing={("configmaps", NAMESPACE): {"web-config"}, ("secrets", NAMESPACE): set()})
        self.assertEqual(rows, [])

    def test_volume_sources_are_resolved(self):
        dep = healthy_deployment()
        dep["spec"]["template"]["spec"]["volumes"] = [
            {"name": "data", "persistentVolumeClaim": {"claimName": "web-data"}},
            {"name": "tls", "secret": {"secretName": "web-tls"}},
        ]
        rows = analyze([dep], existing={("persistentvolumeclaims", NAMESPACE): set(), ("secrets", NAMESPACE): {"web-tls"}})
        self.assertEqual([r["detail"] for r in rows], ["template.spec.volumes[0].persistentVolumeClaim -> PersistentVolumeClaim/web-data not found"])

    def test_route_parent_and_backend_references(self):
        route = obj(
            "HTTPRoute",
            "web",
            spec={
                "parentRefs": [{"name": "edge"}, {"name": "shared", "namespace": "infra"}],
                "rules": [{"backendRefs": [{"name": "web", "port": 80}, {"kind": "ServiceImport", "name": "remote"}]}],
            },
            status={},
            managed_fields=[managed(60)],
        )
        existing = {
            ("gateways.gateway.networking.k8s.io", NAMESPACE): {"edge"},
            ("gateways.gateway.networking.k8s.io", "infra"): set(),
            ("services", NAMESPACE): set(),
        }
        rows = analyze([route], existing=existing)
        self.assertEqual(
            sorted(r["detail"] for r in rows),
            ["parentRefs -> infra/Gateway/shared not found", "rules[0].backendRefs -> Service/web not found"],
        )

    def test_recent_spec_is_given_time_to_settle(self):
        gw = obj("Gateway", "edge", spec={"listeners": [{"tls": {"certificateRefs": [{"name": "edge-tls"}]}}]}, status={}, managed_fields=[managed(3)])
        self.assertEqual(analyze([gw], existing={("secrets", NAMESPACE): set()}), [])
        self.assertEqual(len(analyze([gw], existing={("secrets", NAMESPACE): set()}, override=1)), 1)

    def test_unreadable_referent_kind_is_not_reported(self):
        gw = obj("Gateway", "edge", spec={"listeners": [{"tls": {"certificateRefs": [{"name": "edge-tls"}]}}]}, status={}, managed_fields=[managed(50)])
        self.assertEqual(analyze([gw], existing={}), [])

    def test_secret_contents_are_never_requested(self):
        seen = []

        def lister(resource, ns):
            seen.append(resource)
            return set()

        resolver = stall_report.NameResolver(lister=lister)
        gw = obj("Gateway", "edge", spec={"listeners": [{"tls": {"certificateRefs": [{"name": "edge-tls"}]}}]}, status={}, managed_fields=[managed(50)])
        stall_report.analyze([gw], [], resolver, NOW)
        self.assertEqual(seen, ["secrets"])


class HealthyNamespace(unittest.TestCase):
    def test_no_rows_and_zero_count(self):
        pod = obj("Pod", "web-1", spec={}, status={"phase": "Running", "conditions": [condition("Ready", "True", 300)]})
        cm = obj("ConfigMap", "web-config")
        rs = obj("ReplicaSet", "web-abc", spec={"replicas": 2}, status={"observedGeneration": 1, "replicas": 2})
        objects = [healthy_deployment(), pod, cm, rs]
        events = [warning("Pod", "web-1", 2, first_ago=200, last_ago=190, reason="Unhealthy")]
        rows = analyze(objects, events=events, existing={("configmaps", NAMESPACE): {"web-config"}})
        self.assertEqual(rows, [])
        self.assertEqual(stall_report.stalled_object_count(rows), 0)
        self.assertEqual(stall_report.render_table(rows), "OBJECT  HEURISTIC  DETAIL  STALLED_FOR")

    def test_table_caps_the_detail_column_but_the_finding_keeps_it(self):
        gw = obj("Gateway", "edge", spec={}, status={})
        event = warning("Gateway", "edge", 9, first_ago=40, last_ago=1)
        event["message"] = "x" * (stall_report.TABLE_DETAIL_MAX_CHARS + 50)
        rows = analyze([gw], events=[event])
        self.assertEqual(len(rows[0]["detail"]), len("SYNC x9: ") + stall_report.TABLE_DETAIL_MAX_CHARS + 50)
        cell = stall_report.render_table(rows).split("\n")[1].split("  ")[2]
        self.assertEqual(len(cell), stall_report.TABLE_DETAIL_MAX_CHARS)
        self.assertTrue(cell.endswith(stall_report.TRUNCATION_MARKER))

    def test_count_is_per_object_not_per_row(self):
        dep = healthy_deployment()
        dep["status"]["conditions"] = [condition("Available", "False", 30), condition("Progressing", "False", 30)]
        rows = analyze([dep])
        self.assertEqual(len(rows), 2)
        self.assertEqual(stall_report.stalled_object_count(rows), 1)


class Helpers(unittest.TestCase):
    def test_format_duration(self):
        self.assertEqual(stall_report.format_duration(30), "<1m")
        self.assertEqual(stall_report.format_duration(15 * 60), "15m")
        self.assertEqual(stall_report.format_duration(3 * 3600 + 5 * 60), "3h05m")
        self.assertEqual(stall_report.format_duration(2 * 86400 + 3600), "2d1h")

    def test_parse_time_handles_z_and_fractions(self):
        self.assertEqual(stall_report.parse_time("2026-09-09T12:00:00Z"), NOW)
        self.assertEqual(stall_report.parse_time("2026-09-09T12:00:00.123456Z"), NOW + timedelta(microseconds=123456))
        self.assertIsNone(stall_report.parse_time(None))
        self.assertIsNone(stall_report.parse_time("yesterday"))

    def test_namespaced_resources_filters_exclusions(self):
        listing = (
            "deployments.apps\nevents\nevents.events.k8s.io\nsecrets\nendpoints\npods.metrics.k8s.io\n"
            "configmaps\ncontrollerrevisions.apps\nendpointslices.discovery.k8s.io\nleases.coordination.k8s.io\n"
            "gateways.gateway.networking.k8s.io\n"
        )
        with patch.object(stall_report, "run_kubectl", return_value=(0, listing, "")):
            self.assertEqual(stall_report.namespaced_resources(), ["deployments.apps", "gateways.gateway.networking.k8s.io"])

    def test_namespaced_resources_keeps_a_partial_listing(self):
        stderr = "error: unable to retrieve the complete list of server APIs: external.metrics.k8s.io/v1beta1: the server is currently unable to handle the request"
        with patch.object(stall_report, "run_kubectl", return_value=(1, "deployments.apps\npods\n", stderr)):
            with redirect_stderr(io.StringIO()) as err:
                self.assertEqual(stall_report.namespaced_resources(), ["deployments.apps", "pods"])
        self.assertIn("warning: error: unable to retrieve", err.getvalue())
        with patch.object(stall_report, "run_kubectl", return_value=(1, "", "forbidden")):
            with self.assertRaises(RuntimeError):
                stall_report.namespaced_resources()

    def test_kubectl_json_keeps_partial_output(self):
        partial = '{"kind":"List","items":[{"kind":"Deployment"}]}'
        with patch.object(stall_report, "run_kubectl", return_value=(1, partial, "error: the server could not find the requested resource")):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(stall_report.kubectl_json(["get", "x"])["items"], [{"kind": "Deployment"}])
        with patch.object(stall_report, "run_kubectl", return_value=(1, "", "boom")):
            with self.assertRaises(RuntimeError):
                stall_report.kubectl_json(["get", "x"])

    def test_kubectl_names_strips_the_resource_prefix(self):
        with patch.object(stall_report, "run_kubectl", return_value=(0, "secret/a\nsecret/b\n", "")):
            self.assertEqual(stall_report.kubectl_names("secrets", NAMESPACE), {"a", "b"})
        with patch.object(stall_report, "run_kubectl", return_value=(1, "", "forbidden")):
            self.assertIsNone(stall_report.kubectl_names("secrets", NAMESPACE))


class Exempt(unittest.TestCase):
    """The stall watch's opt-out annotation marks the rows; it hides nothing here."""

    @staticmethod
    def wedged(name, annotations=None):
        dep = healthy_deployment(name)
        dep["status"]["conditions"] = [condition("Progressing", "False", 30, "ProgressDeadlineExceeded")]
        if annotations is not None:
            dep["metadata"]["annotations"] = annotations
        return dep

    def run_main(self, objects, *argv):
        with patch.object(stall_report, "collect", return_value=(objects, [])):
            with patch.object(stall_report, "NameResolver", lambda: FakeResolver({})):
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(stall_report.main(["--namespace", NAMESPACE, *argv]), 0)
        return out.getvalue()

    def test_the_exempt_field_comes_from_the_objects_annotation(self):
        rows = analyze([
            self.wedged("exempt", {stall_report.EXEMPT_ANNOTATION: stall_report.EXEMPT_VALUE}),
            self.wedged("other-value", {stall_report.EXEMPT_ANNOTATION: "yes"}),
            self.wedged("unrelated", {"team": "payments"}),
            self.wedged("bare"),
        ])
        self.assertEqual({r["object"]: r[stall_report.EXEMPT_FIELD] for r in rows}, {
            "Deployment/exempt": True,
            "Deployment/other-value": False,
            "Deployment/unrelated": False,
            "Deployment/bare": False,
        })

    def test_json_rows_carry_the_field_and_the_count_is_unchanged(self):
        report = json.loads(self.run_main([self.wedged("web", {stall_report.EXEMPT_ANNOTATION: stall_report.EXEMPT_VALUE})], "--json"))
        self.assertEqual(report["stalled_resources"], 1)
        self.assertIs(report["findings"][0][stall_report.EXEMPT_FIELD], True)

    def test_the_table_is_the_same_with_or_without_the_annotation(self):
        exempt = self.run_main([self.wedged("web", {stall_report.EXEMPT_ANNOTATION: stall_report.EXEMPT_VALUE})])
        plain = self.run_main([self.wedged("web")])
        self.assertEqual(exempt, plain)
        self.assertEqual(exempt.splitlines()[0].split(), list(stall_report.TABLE_COLUMNS))
        self.assertEqual(exempt.splitlines()[-1], "stalled resources: 1")


class Main(unittest.TestCase):
    def test_table_output_ends_with_the_summary_line(self):
        dep = healthy_deployment()
        dep["status"]["conditions"] = [condition("Progressing", "False", 30, "ProgressDeadlineExceeded")]
        with patch.object(stall_report, "collect", return_value=([dep], [])):
            with patch.object(stall_report, "NameResolver", lambda: FakeResolver({})):
                out = io.StringIO()
                with redirect_stdout(out):
                    rc = stall_report.main(["--namespace", NAMESPACE])
        self.assertEqual(rc, 0)
        lines = out.getvalue().rstrip("\n").split("\n")
        self.assertTrue(lines[0].startswith("OBJECT"))
        self.assertIn("Deployment/web", lines[1])
        self.assertEqual(lines[-1], "stalled resources: 1")

    def test_json_output(self):
        with patch.object(stall_report, "collect", return_value=([healthy_deployment()], [])):
            with patch.object(stall_report, "NameResolver", lambda: FakeResolver({})):
                out = io.StringIO()
                with redirect_stdout(out):
                    rc = stall_report.main(["--namespace", NAMESPACE, "--json"])
        self.assertEqual(rc, 0)
        self.assertIn('"stalled_resources": 0', out.getvalue())

    def test_collect_failure_exits_two(self):
        with patch.object(stall_report, "collect", side_effect=RuntimeError("no cluster")):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(stall_report.main(["--namespace", NAMESPACE]), 2)

    def test_collect_wraps_a_single_object(self):
        single = {"kind": "Deployment", "metadata": {"name": "foo"}}
        with patch.object(stall_report, "kubectl_json", side_effect=[single, {"items": []}]):
            objects, events = stall_report.collect(NAMESPACE, "deployments/foo")
        self.assertEqual(objects, [single])
        self.assertEqual(events, [])

    def test_collect_reads_one_kind_per_call_and_only_warning_events(self):
        calls = []

        def fake_json(args):
            calls.append(args)
            return {"items": [{"kind": args[1]}]}

        with patch.object(stall_report, "kubectl_json", side_effect=fake_json):
            objects, events = stall_report.collect(NAMESPACE, "deployments,gateways,")
        self.assertEqual(calls[0], ["get", "deployments", "-n", NAMESPACE, "--show-managed-fields"])
        self.assertEqual(calls[1], ["get", "gateways", "-n", NAMESPACE, "--show-managed-fields"])
        self.assertEqual(calls[2], ["get", "events", "-n", NAMESPACE, "--field-selector", "type=Warning"])
        self.assertEqual(len(calls), 3)
        self.assertEqual([o["kind"] for o in objects], ["deployments", "gateways"])
        self.assertEqual(events, [{"kind": "events"}])

    def test_a_listing_the_shim_cut_with_exit_74_is_a_warning_the_same_way(self):
        """The credential-proxy shim exits 74 on a cut reply (the counts after
        its marker); the cut kind is still skipped with a warning and the
        other kinds still count."""
        cut = '{"apiVersion":"v1","kind":"List","items":[{"kind":"Pod","metadata":{"name":"a'

        def fake_kubectl(args):
            if args[1] == "pods":
                return 74, cut, "credential proxy output truncated at 8388608 bytes (kept 8388608 bytes of stdout; exit 74)\n"
            if args[1] == "events":
                return 0, '{"items":[]}', ""
            return 0, '{"items":[{"kind":"Deployment","metadata":{"name":"web"}}]}', ""

        with patch.object(stall_report, "run_kubectl", side_effect=fake_kubectl):
            with redirect_stderr(io.StringIO()) as err:
                objects, events = stall_report.collect(NAMESPACE, "deployments,pods,replicasets")
        self.assertEqual([o["metadata"]["name"] for o in objects], ["web", "web"])
        self.assertIn("warning: credential proxy output truncated at 8388608 bytes", err.getvalue())
        self.assertIn(f"warning: pods in {NAMESPACE} not scanned; its objects are missing from the count", err.getvalue())

    def test_collect_keeps_the_other_kinds_when_one_listing_is_cut(self):
        """The sandbox's kubectl is the credential-proxy shim, which returned
        the prefix of an over-cap reply with exit 0 and a stderr note; it now
        exits 74, and this is the older shim's shape."""
        cut = '{"apiVersion":"v1","kind":"List","items":[{"kind":"Pod","metadata":{"name":"a'

        def fake_kubectl(args):
            if args[1] == "pods":
                return 0, cut, "credential proxy output truncated\n"
            if args[1] == "events":
                return 0, '{"items":[]}', ""
            return 0, '{"items":[{"kind":"Deployment","metadata":{"name":"web"}}]}', ""

        with patch.object(stall_report, "run_kubectl", side_effect=fake_kubectl):
            with redirect_stderr(io.StringIO()) as err:
                objects, events = stall_report.collect(NAMESPACE, "deployments,pods,replicasets")
        self.assertEqual([o["metadata"]["name"] for o in objects], ["web", "web"])
        self.assertEqual(events, [])
        self.assertIn("warning: credential proxy output truncated", err.getvalue())
        self.assertIn(f"warning: pods in {NAMESPACE} not scanned; its objects are missing from the count", err.getvalue())
        self.assertNotIn("deployments in", err.getvalue())

    def test_collect_errors_only_when_no_kind_could_be_read(self):
        with patch.object(stall_report, "run_kubectl", return_value=(1, "", "Unable to connect to the server")):
            with redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(RuntimeError) as raised:
                    stall_report.collect(NAMESPACE, "deployments,pods")
        self.assertIn("no kind could be read", str(raised.exception))
        self.assertIn("deployments, pods", str(raised.exception))
        self.assertIn("warning: pods in", err.getvalue())

    def test_collect_scans_without_events_when_they_cannot_be_read(self):
        def fake_kubectl(args):
            if args[1] == "events":
                return 0, '{"items":[{"kind":"Event","metadata":{"name":"x', "credential proxy output truncated\n"
            return 0, '{"items":[{"kind":"Deployment","metadata":{"name":"web"}}]}', ""

        with patch.object(stall_report, "run_kubectl", side_effect=fake_kubectl):
            with redirect_stderr(io.StringIO()) as err:
                objects, events = stall_report.collect(NAMESPACE, "deployments")
        self.assertEqual(len(objects), 1)
        self.assertEqual(events, [])
        self.assertIn(f"warning: events in {NAMESPACE} not read; repeating-warnings is not checked", err.getvalue())


# --------------------------------------------------------------------------
# cluster mode: recorded kubectl output, split per namespace
# --------------------------------------------------------------------------

BILLING = "billing"
OTHER = "other"
GATEWAYS = "gateways.gateway.networking.k8s.io"
CLUSTER_KINDS = f"deployments,{GATEWAYS}"
CUT_STDERR = "credential proxy output truncated at 8388608 bytes (kept 8388608 bytes of stdout; exit 74)\n"


def in_namespace(o: dict, namespace: str) -> dict:
    out = json.loads(json.dumps(o))
    out["metadata"]["namespace"] = namespace
    return out


def lagging_deployment(name="web", namespace=NAMESPACE) -> dict:
    """generation 1 ahead of observedGeneration 0 for half an hour, and an
    envFrom naming a ConfigMap that is not there: two rows."""
    return in_namespace(
        obj(
            "Deployment",
            name,
            spec={"replicas": 1, "template": {"spec": {"containers": [{"name": "app", "envFrom": [{"configMapRef": {"name": "feature-flags"}}]}]}}},
            status={"observedGeneration": 0, "conditions": [condition("Available", "True", 300, "MinimumReplicasAvailable")]},
            managed_fields=[managed(30)],
        ),
        namespace,
    )


def deadline_deployment(name="batch", namespace=BILLING) -> dict:
    return in_namespace(
        obj(
            "Deployment",
            name,
            spec={"replicas": 1},
            status={"observedGeneration": 1, "conditions": [condition("Progressing", "False", 40, "ProgressDeadlineExceeded")]},
            managed_fields=[managed(300)],
        ),
        namespace,
    )


def stale_gateway(name="edge", namespace=NAMESPACE) -> dict:
    return in_namespace(
        obj(
            "Gateway",
            name,
            spec={"gatewayClassName": "gke-l7", "listeners": [{"name": "https", "tls": {"certificateRefs": [{"name": "edge-tls"}]}}]},
            status={"listeners": [{"name": "https", "conditions": [condition("ResolvedRefs", "False", 50, "InvalidCertificateRef")]}]},
            managed_fields=[managed(300)],
        ),
        namespace,
    )


def raw_item(o: dict) -> dict:
    """An item as the raw API lists it: no kind or apiVersion of its own."""
    out = json.loads(json.dumps(o))
    out.pop("kind", None)
    out.pop("apiVersion", None)
    return out


class RecordedCluster:
    """A cluster of three namespaces as kubectl prints it, answering both the
    cluster-wide reads of cluster mode and the per-namespace reads of the
    namespace path from the same objects, so the two paths can be compared.
    `cut` names kinds whose chunked reply the broker cut; `raw_cut_until`
    is the page size at and above which a raw deployments page is cut;
    `raw_rc` fails every raw deployments page; `events_cut` cuts the raw
    and the chunked events reads."""

    def __init__(self, cut=(), raw_cut_until=0, raw_rc=0, events_cut=False, secrets_rc=1):
        self.cut = set(cut)
        self.raw_cut_until = raw_cut_until
        self.raw_rc = raw_rc
        self.events_cut = events_cut
        self.secrets_rc = secrets_rc
        self.calls = []
        self.deployments = [lagging_deployment(), healthy_deployment("api"), deadline_deployment(), deadline_deployment("hidden", OTHER)]
        self.gateways = [stale_gateway()]
        self.events = [self.event("web", NAMESPACE), self.event("hidden", OTHER)]
        self.configmaps = {(NAMESPACE, "present"), (BILLING, "cfg")}

    @staticmethod
    def event(name, namespace):
        event = warning("Deployment", name, 5, 30, 1)
        event["metadata"] = {"name": f"{name}.18a", "namespace": namespace}
        event["involvedObject"]["namespace"] = namespace
        return event

    def listing(self, items):
        return json.dumps({"apiVersion": "v1", "kind": "List", "items": items})

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:2] == ["get", "--raw"]:
            url = stall_report.urllib.parse.urlparse(args[2])
            query = dict(stall_report.urllib.parse.parse_qsl(url.query))
            if url.path == "/apis/apps/v1/deployments":
                if self.raw_rc:
                    return self.raw_rc, "", "error: the server does not allow this method"
                if int(query["limit"]) >= self.raw_cut_until > 0:
                    return 74, '{"kind":"DeploymentList","items":[{"metadata":{"name":"a', CUT_STDERR
                first, second = [raw_item(d) for d in self.deployments[:2]], [raw_item(d) for d in self.deployments[2:]]
                if query.get("continue"):
                    return 0, json.dumps({"kind": "DeploymentList", "apiVersion": "apps/v1", "metadata": {}, "items": second}), ""
                return 0, json.dumps({"kind": "DeploymentList", "apiVersion": "apps/v1", "metadata": {"continue": "tok1"}, "items": first}), ""
            if url.path == "/api/v1/events":
                assert query.get("fieldSelector") == "type=Warning", query
                if self.events_cut:
                    return 74, "", CUT_STDERR
                return 0, json.dumps({"kind": "EventList", "apiVersion": "v1", "metadata": {}, "items": [raw_item(e) for e in self.events]}), ""
            raise AssertionError(f"unexpected raw read {args}")
        resource = args[1]
        if "--all-namespaces" in args:
            assert stall_report.CHUNK_SIZE_ARG in args, args
            if args[-1] == stall_report.NAME_COLUMNS:
                if resource == "secrets":
                    return self.secrets_rc, "", "Error from server (Forbidden): secrets is forbidden"
                assert resource == "configmaps", args
                return 0, "".join(f"{ns}   {name}\n" for ns, name in sorted(self.configmaps)), ""
            if resource in self.cut:
                return 74, '{"kind":"List","items":[{"metadata":{"name":"a', CUT_STDERR
            if resource == GATEWAYS:
                return 0, self.listing(self.gateways), ""
            if resource == "events":
                if self.events_cut:
                    return 74, "", CUT_STDERR
                return 0, self.listing(self.events), ""
            raise AssertionError(f"unexpected cluster-wide read {args}")
        namespace = args[args.index("-n") + 1]
        if args[-1] == "name":
            if resource == "secrets":
                return self.secrets_rc, "", "Error from server (Forbidden): secrets is forbidden"
            return 0, "".join(f"configmap/{name}\n" for ns, name in sorted(self.configmaps) if ns == namespace), ""
        if resource == "deployments":
            return 0, self.listing([d for d in self.deployments if d["metadata"]["namespace"] == namespace]), ""
        if resource == GATEWAYS:
            return 0, self.listing([g for g in self.gateways if g["metadata"]["namespace"] == namespace]), ""
        if resource == "events":
            return 0, self.listing([e for e in self.events if e["involvedObject"]["namespace"] == namespace]), ""
        raise AssertionError(f"unexpected namespaced read {args}")

    def namespace_path(self, namespace):
        """The findings the per-namespace path gives, from the same objects."""
        with patch.object(stall_report, "run_kubectl", self):
            objects, events = stall_report.collect(namespace, CLUSTER_KINDS)
            return stall_report.analyze(objects, events, stall_report.NameResolver(), NOW)


class ClusterMode(unittest.TestCase):
    def report(self, cluster, namespaces=(NAMESPACE, BILLING), kinds=CLUSTER_KINDS, extra=None):
        with patch.object(stall_report, "run_kubectl", cluster):
            with redirect_stderr(io.StringIO()) as err:
                report = stall_report.report_cluster(list(namespaces), kinds, extra or {}, NOW)
        return report, err.getvalue()

    def test_each_namespace_gets_the_findings_the_namespace_read_gives(self):
        cluster = RecordedCluster()
        report, _ = self.report(cluster)
        cluster_reads = len(cluster.calls)
        self.assertEqual(sorted(report["namespaces"]), [BILLING, NAMESPACE], "the namespace not asked for is not reported")
        for namespace in (NAMESPACE, BILLING):
            with self.subTest(namespace=namespace):
                expected = RecordedCluster().namespace_path(namespace)
                self.assertEqual(json.dumps(report["namespaces"][namespace]["findings"]), json.dumps(expected))
                self.assertEqual(report["namespaces"][namespace]["stalled_resources"], stall_report.stalled_object_count(expected))
        heuristics = sorted(f["heuristic"] for f in report["namespaces"][NAMESPACE]["findings"])
        self.assertEqual(heuristics, ["dangling-reference", "generation-lag", "repeating-warnings", "stale-condition"], "every heuristic fires from the cluster-wide read")
        self.assertEqual([f["object"] for f in report["namespaces"][BILLING]["findings"]], ["Deployment/batch"])
        self.assertEqual(report["fallback"], [])
        self.assertFalse(report["events_fallback"])
        # Two raw pages of deployments, one chunked read of gateways, one raw
        # page of events, and one listing each of configmaps (the envFrom)
        # and secrets (the listener's certificateRefs; forbidden, so
        # unresolved) for the dangling references: six calls for the
        # cluster, against five per namespace.
        self.assertEqual(cluster_reads, 6, cluster.calls)
        self.assertEqual(cluster.calls[0], ["get", "--raw", "/apis/apps/v1/deployments?limit=500"])
        self.assertEqual(cluster.calls[1], ["get", "--raw", "/apis/apps/v1/deployments?limit=500&continue=tok1"])
        self.assertEqual(cluster.calls[2], ["get", GATEWAYS, "--all-namespaces", "--chunk-size=500", "--show-managed-fields", "-o", "json"])
        self.assertEqual(cluster.calls[3], ["get", "--raw", "/api/v1/events?limit=500&fieldSelector=type%3DWarning"])
        self.assertEqual(cluster.calls[4], ["get", "configmaps", "--all-namespaces", "--chunk-size=500", "--no-headers", "-o", stall_report.NAME_COLUMNS])
        self.assertEqual(cluster.calls[5][:2], ["get", "secrets"])
        self.assertEqual(cluster.calls[5][-1], stall_report.NAME_COLUMNS, "names only: no Secret body crosses")

    def test_a_chunked_reply_the_broker_cut_leaves_the_kind_to_the_per_namespace_read(self):
        report, err = self.report(RecordedCluster(cut=[GATEWAYS]))
        self.assertEqual(report["fallback"], [GATEWAYS])
        self.assertNotIn("Gateway/edge", [f["object"] for f in report["namespaces"][NAMESPACE]["findings"]])
        self.assertIn("Deployment/web", [f["object"] for f in report["namespaces"][NAMESPACE]["findings"]], "the kinds read whole are still analysed")
        self.assertIn(f"warning: {GATEWAYS} across every namespace was cut; read per namespace instead", err)

    def test_a_cut_raw_page_is_asked_for_again_at_half_the_size(self):
        cluster = RecordedCluster(raw_cut_until=500)
        report, _ = self.report(cluster)
        self.assertEqual(report["fallback"], [])
        pages = [c[2] for c in cluster.calls if "deployments" in c[2]]
        self.assertEqual(pages[:2], ["/apis/apps/v1/deployments?limit=500", "/apis/apps/v1/deployments?limit=250"])
        self.assertEqual(len(report["namespaces"][BILLING]["findings"]), 1)

    def test_a_raw_page_cut_at_the_smallest_size_falls_back_and_a_failed_raw_read_does_too(self):
        cluster = RecordedCluster(raw_cut_until=1)
        report, err = self.report(cluster)
        self.assertEqual(report["fallback"], ["deployments"])
        sizes = [int(c[2].rsplit("limit=", 1)[1].split("&")[0]) for c in cluster.calls if "deployments" in c[2]]
        self.assertEqual(sizes, [500, 250, 125, 62, 31], "halved down to the floor, then given up")
        self.assertIn("cut at a page of 31; read per namespace instead", err)
        report, err = self.report(RecordedCluster(raw_rc=1))
        self.assertEqual(report["fallback"], ["deployments"])
        self.assertIn("/apis/apps/v1/deployments page failed (1); read per namespace instead", err)
        self.assertEqual([f["object"] for f in report["namespaces"][NAMESPACE]["findings"]], ["Gateway/edge"])

    def test_events_no_cluster_wide_read_could_read_whole_are_said_so(self):
        cluster = RecordedCluster(events_cut=True)
        report, _ = self.report(cluster)
        self.assertTrue(report["events_fallback"])
        self.assertEqual([c[1] for c in cluster.calls if c[1] == "events"], ["events"], "the chunked read is tried after the raw one")
        self.assertNotIn("repeating-warnings", [f["heuristic"] for f in report["namespaces"][NAMESPACE]["findings"]])

    def test_an_object_named_with_its_kind_is_left_to_the_per_namespace_read(self):
        cluster = RecordedCluster()
        report, _ = self.report(cluster, kinds="deployments/web")
        self.assertEqual(report["fallback"], ["deployments/web"])
        self.assertEqual([c for c in cluster.calls if "deployments" in c[-1] or c[1] == "deployments/web"], [], "no cluster-wide read of one object")

    def test_an_empty_kind_list_reads_no_kind_and_the_extras_are_read_where_asked(self):
        cluster = RecordedCluster()
        report, _ = self.report(cluster, kinds="", extra={BILLING: ["deployments"]})
        self.assertEqual(report["namespaces"][NAMESPACE]["findings"], [], "payments asked for nothing")
        self.assertEqual([f["object"] for f in report["namespaces"][BILLING]["findings"]], ["Deployment/batch"])
        self.assertEqual([c[1] for c in cluster.calls if c[1] == GATEWAYS], [])

    def test_the_cluster_resolver_lists_a_referent_kind_once_and_an_unlistable_one_is_unresolved(self):
        calls = []

        def lister(resource):
            calls.append(resource)
            return None if resource == "secrets" else {(NAMESPACE, "present")}

        resolver = stall_report.ClusterNameResolver(lister=lister)
        self.assertTrue(resolver.exists("ConfigMap", NAMESPACE, "present"))
        self.assertFalse(resolver.exists("ConfigMap", BILLING, "present"), "the namespace is part of the name")
        self.assertFalse(resolver.exists("ConfigMap", NAMESPACE, "absent"))
        self.assertIsNone(resolver.exists("Secret", NAMESPACE, "tls"))
        self.assertIsNone(resolver.exists("Secret", BILLING, "tls"))
        self.assertIsNone(resolver.exists("Thing", NAMESPACE, "x"), "a kind with no resource is left alone")
        self.assertEqual(calls, ["configmaps", "secrets"])

    def test_a_cut_referent_listing_leaves_references_unresolved(self):
        with patch.object(stall_report, "run_kubectl", return_value=(0, "payments   present\n", "credential proxy output truncated\n")):
            with redirect_stderr(io.StringIO()):
                self.assertIsNone(stall_report.kubectl_names_cluster_wide("configmaps"))
        with patch.object(stall_report, "run_kubectl", return_value=(0, "payments   present\nbilling   cfg\n", "")):
            self.assertEqual(stall_report.kubectl_names_cluster_wide("configmaps"), {("payments", "present"), ("billing", "cfg")})

    def test_extra_kinds_are_parsed_per_namespace(self):
        self.assertEqual(stall_report.parse_extra_kinds(["billing=a,b", "billing=c", "web=d"]), {"billing": ["a", "b", "c"], "web": ["d"]})
        with self.assertRaises(ValueError):
            stall_report.parse_extra_kinds(["billing"])

    def test_main_cluster_mode_prints_the_report_and_needs_json(self):
        report = {"namespaces": {NAMESPACE: {"namespace": NAMESPACE, "stalled_resources": 0, "findings": []}}, "fallback": [], "events_fallback": False}
        with patch.object(stall_report, "report_cluster", return_value=report) as mocked:
            with redirect_stdout(io.StringIO()) as out:
                rc = stall_report.main(["--namespaces", f"{NAMESPACE},{BILLING}", "--kind", "deployments", "--extra-kinds", f"{BILLING}=jobs", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue()), report)
        self.assertEqual(mocked.call_args.args[:3], ([NAMESPACE, BILLING], "deployments", {BILLING: ["jobs"]}))
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                stall_report.main(["--namespaces", NAMESPACE])
            with self.assertRaises(SystemExit):
                stall_report.main(["--namespace", NAMESPACE, "--extra-kinds", "a=b", "--json"])
        with patch.object(stall_report, "report_cluster", side_effect=RuntimeError("kubectl api-resources failed")):
            with redirect_stderr(io.StringIO()):
                self.assertEqual(stall_report.main(["--namespaces", NAMESPACE, "--json"]), 2)


if __name__ == "__main__":
    unittest.main()
