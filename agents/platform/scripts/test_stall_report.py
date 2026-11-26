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
        with patch.object(stall_report, "run_kubectl", return_value=(1, "", "Error from server (ServiceUnavailable): the server is currently unable to handle the request")):
            self.assertIsNone(stall_report.kubectl_names("secrets", NAMESPACE), "failed for the moment: None")
        for refusal in ("Error from server (Forbidden): secrets is forbidden: User cannot list resource", 'error: the server doesn\'t have a resource type "gateways"'):
            with patch.object(stall_report, "run_kubectl", return_value=(1, "", refusal)):
                with redirect_stderr(io.StringIO()):
                    self.assertIs(stall_report.kubectl_names("secrets", NAMESPACE), stall_report.REFUSED, "refused for good: REFUSED")
        resolver = stall_report.NameResolver(lister=lambda resource, ns: stall_report.REFUSED)
        self.assertIsNone(resolver.exists("Secret", NAMESPACE, "tls"), "a refused kind is unresolvable, as an unreadable one is")


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

# The second namespace the cluster-mode tests ask for, beside NAMESPACE. Not
# named for its value: CodeQL's private-data heuristic reads `billing` in a
# variable's name as a source, and the script's stderr warnings as sinks.
SECOND_NAMESPACE = "billing"
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


def deadline_deployment(name="batch", namespace=SECOND_NAMESPACE) -> dict:
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

    def __init__(self, cut=(), raw_cut_until=0, raw_rc=0, events_cut=False, secrets_rc=1, configmaps_rc=0, namespace_configmaps=None, billing_reference=False):
        self.cut = set(cut)
        self.raw_cut_until = raw_cut_until
        self.raw_rc = raw_rc
        self.events_cut = events_cut
        self.secrets_rc = secrets_rc
        # The cluster-wide configmaps listing's exit code; the per-namespace one always answers.
        self.configmaps_rc = configmaps_rc
        # Per namespace, what its own configmaps listing does: "ok" (the
        # default), "transient" (a 503 this time) or "forbidden".
        self.namespace_configmaps = dict(namespace_configmaps or {})
        # Give billing's Deployment an envFrom naming a ConfigMap billing does
        # not have, so billing carries a dangling reference too.
        self.billing_reference = billing_reference
        self.calls = []
        self.deployments = [lagging_deployment(), healthy_deployment("api"), deadline_deployment(), deadline_deployment("hidden", OTHER)]
        if billing_reference:
            self.deployments[2]["spec"]["template"] = {"spec": {"containers": [{"name": "app", "envFrom": [{"configMapRef": {"name": "feature-flags"}}]}]}}
        self.gateways = [stale_gateway()]
        self.events = [self.event("web", NAMESPACE), self.event("hidden", OTHER)]
        self.configmaps = {(NAMESPACE, "present"), (SECOND_NAMESPACE, "cfg")}

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
                if self.configmaps_rc:
                    return self.configmaps_rc, "", "Error from server (ServiceUnavailable): the server is currently unable to handle the request"
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
            mode = self.namespace_configmaps.get(namespace, "ok")
            if mode == "transient":
                return 1, "", "Error from server (ServiceUnavailable): the server is currently unable to handle the request"
            if mode == "forbidden":
                return 1, "", "Error from server (Forbidden): configmaps is forbidden"
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
    def report(self, cluster, namespaces=(NAMESPACE, SECOND_NAMESPACE), kinds=CLUSTER_KINDS, extra=None):
        with patch.object(stall_report, "run_kubectl", cluster):
            with redirect_stderr(io.StringIO()) as err:
                report = stall_report.report_cluster(list(namespaces), kinds, extra or {}, NOW)
        return report, err.getvalue()

    def test_each_namespace_gets_the_findings_the_namespace_read_gives(self):
        cluster = RecordedCluster()
        report, _ = self.report(cluster)
        cluster_reads = len(cluster.calls)
        self.assertEqual(sorted(report["namespaces"]), sorted([NAMESPACE, SECOND_NAMESPACE]), "the namespace not asked for is not reported")
        for namespace in (NAMESPACE, SECOND_NAMESPACE):
            with self.subTest(namespace=namespace):
                expected = RecordedCluster().namespace_path(namespace)
                self.assertEqual(json.dumps(report["namespaces"][namespace]["findings"]), json.dumps(expected))
                self.assertEqual(report["namespaces"][namespace]["stalled_resources"], stall_report.stalled_object_count(expected))
        heuristics = sorted(f["heuristic"] for f in report["namespaces"][NAMESPACE]["findings"])
        self.assertEqual(heuristics, ["dangling-reference", "generation-lag", "repeating-warnings", "stale-condition"], "every heuristic fires from the cluster-wide read")
        self.assertEqual([f["object"] for f in report["namespaces"][SECOND_NAMESPACE]["findings"]], ["Deployment/batch"])
        self.assertEqual(report["fallback"], [])
        self.assertFalse(report["events_fallback"])
        # Two raw pages of deployments, one chunked read of gateways, one raw
        # page of events, and one listing each of configmaps (the envFrom)
        # and secrets (the listener's certificateRefs) for the dangling
        # references, the secrets listing forbidden across every namespace
        # and so tried once per namespace with a reference, forbidden there
        # too: seven calls for the cluster, against five per namespace.
        self.assertEqual(cluster_reads, 7, cluster.calls)
        self.assertEqual(cluster.calls[0], ["get", "--raw", "/apis/apps/v1/deployments?limit=500"])
        self.assertEqual(cluster.calls[1], ["get", "--raw", "/apis/apps/v1/deployments?limit=500&continue=tok1"])
        self.assertEqual(cluster.calls[2], ["get", GATEWAYS, "--all-namespaces", "--chunk-size=500", "--show-managed-fields", "-o", "json"])
        self.assertEqual(cluster.calls[3], ["get", "--raw", "/api/v1/events?limit=500&fieldSelector=type%3DWarning"])
        self.assertEqual(cluster.calls[4], ["get", "configmaps", "--all-namespaces", "--chunk-size=500", "--no-headers", "-o", stall_report.NAME_COLUMNS])
        self.assertEqual(cluster.calls[5][:2], ["get", "secrets"])
        self.assertEqual(cluster.calls[5][-1], stall_report.NAME_COLUMNS, "names only: no Secret body crosses")
        self.assertEqual(cluster.calls[6], ["get", "secrets", "-n", NAMESPACE, "-o", "name"], "the per-namespace listing a failed cluster-wide one falls back to")

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
        self.assertEqual(len(report["namespaces"][SECOND_NAMESPACE]["findings"]), 1)

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
        report, _ = self.report(cluster, kinds="", extra={SECOND_NAMESPACE: ["deployments"]})
        self.assertEqual(report["namespaces"][NAMESPACE]["findings"], [], "payments asked for nothing")
        self.assertEqual([f["object"] for f in report["namespaces"][SECOND_NAMESPACE]["findings"]], ["Deployment/batch"])
        self.assertEqual([c[1] for c in cluster.calls if c[1] == GATEWAYS], [])

    def test_a_failed_cluster_wide_referent_listing_falls_back_to_the_per_namespace_listing(self):
        # The configmaps listing across every namespace fails (an aggregated
        # API hiccup, a cut): the references are resolved per namespace, as
        # the namespace read resolves them, and the dangling-reference row
        # stands rather than clearing on a listing that failed.
        cluster = RecordedCluster(configmaps_rc=1)
        report, err = self.report(cluster)
        expected = RecordedCluster().namespace_path(NAMESPACE)
        self.assertEqual(json.dumps(report["namespaces"][NAMESPACE]["findings"]), json.dumps(expected))
        self.assertIn("dangling-reference", [f["heuristic"] for f in report["namespaces"][NAMESPACE]["findings"]])
        self.assertIn("references to configmaps are resolved per namespace instead", err)
        self.assertEqual([c for c in cluster.calls if c[1] == "configmaps" and "-n" in c], [["get", "configmaps", "-n", NAMESPACE, "-o", "name"]], "one per-namespace listing, for the namespace with the reference")

    def test_a_kind_resolved_per_namespace_stays_resolved_in_the_next_namespace(self):
        # Two namespaces with a dangling ConfigMap reference, the cluster-wide
        # listing failed: the first namespace's own listing answers, and the
        # second namespace gets its row too. A memo that tripped on any
        # answer would lose the second row.
        cluster = RecordedCluster(configmaps_rc=1, billing_reference=True)
        report, _ = self.report(cluster)
        for namespace in (NAMESPACE, SECOND_NAMESPACE):
            with self.subTest(namespace=namespace):
                self.assertIn("dangling-reference", [f["heuristic"] for f in report["namespaces"][namespace]["findings"]])
                self.assertEqual(json.dumps(report["namespaces"][namespace]["findings"]), json.dumps(RecordedCluster(billing_reference=True).namespace_path(namespace)))
        self.assertEqual([c[3] for c in cluster.calls if c[1] == "configmaps" and "-n" in c], [NAMESPACE, SECOND_NAMESPACE], "one per-namespace listing for each namespace with a reference")

    def test_a_per_namespace_listing_that_fails_for_the_moment_skips_that_namespace_only(self):
        # The first namespace's own listing hits a 503: its reference goes
        # unchecked this tick, with the warning, and the kind is asked for
        # again in the next namespace, which gets its row.
        cluster = RecordedCluster(configmaps_rc=1, namespace_configmaps={NAMESPACE: "transient"}, billing_reference=True)
        report, err = self.report(cluster)
        self.assertNotIn("dangling-reference", [f["heuristic"] for f in report["namespaces"][NAMESPACE]["findings"]])
        self.assertIn(f"cannot list configmaps in {NAMESPACE}; references to configmaps are not checked", err)
        self.assertIn("dangling-reference", [f["heuristic"] for f in report["namespaces"][SECOND_NAMESPACE]["findings"]], "the next namespace still gets its row")
        self.assertEqual([c[3] for c in cluster.calls if c[1] == "configmaps" and "-n" in c], [NAMESPACE, SECOND_NAMESPACE])

    def test_a_per_namespace_listing_refused_outright_is_not_asked_for_again(self):
        cluster = RecordedCluster(configmaps_rc=1, namespace_configmaps={NAMESPACE: "forbidden", SECOND_NAMESPACE: "forbidden"}, billing_reference=True)
        report, _ = self.report(cluster)
        for namespace in (NAMESPACE, SECOND_NAMESPACE):
            self.assertNotIn("dangling-reference", [f["heuristic"] for f in report["namespaces"][namespace]["findings"]])
        self.assertEqual([c[3] for c in cluster.calls if c[1] == "configmaps" and "-n" in c], [NAMESPACE], "refused in the first namespace: not asked for in the second")

    def test_the_cluster_resolver_lists_a_referent_kind_once_and_falls_back_per_namespace_when_it_cannot(self):
        calls = []
        namespaced = []

        def lister(resource):
            calls.append(resource)
            return None if resource in ("secrets", "services") else {(NAMESPACE, "present")}

        def namespace_lister(resource, namespace):
            namespaced.append((resource, namespace))
            if resource == "secrets":
                return stall_report.REFUSED
            if resource == "services" and namespace == NAMESPACE:
                return None  # a 503 this time
            return {"svc"}

        resolver = stall_report.ClusterNameResolver(lister=lister, namespace_lister=namespace_lister)
        self.assertTrue(resolver.exists("ConfigMap", NAMESPACE, "present"))
        self.assertFalse(resolver.exists("ConfigMap", SECOND_NAMESPACE, "present"), "the namespace is part of the name")
        self.assertFalse(resolver.exists("ConfigMap", NAMESPACE, "absent"))
        self.assertIsNone(resolver.exists("Service", NAMESPACE, "svc"), "a per-namespace listing that failed this time leaves this namespace unchecked")
        self.assertTrue(resolver.exists("Service", SECOND_NAMESPACE, "svc"), "and the kind is asked for again in the next namespace")
        self.assertFalse(resolver.exists("Service", SECOND_NAMESPACE, "gone"))
        self.assertIsNone(resolver.exists("Secret", NAMESPACE, "tls"), "a kind refused per namespace too stays unresolved")
        self.assertIsNone(resolver.exists("Secret", SECOND_NAMESPACE, "tls"))
        self.assertIsNone(resolver.exists("Secret", SECOND_NAMESPACE, "other"))
        self.assertIsNone(resolver.exists("Thing", NAMESPACE, "x"), "a kind with no resource is left alone")
        self.assertEqual(calls, ["configmaps", "services", "secrets"], "one cluster-wide listing per kind")
        self.assertEqual(
            namespaced,
            [("services", NAMESPACE), ("services", SECOND_NAMESPACE), ("secrets", NAMESPACE)],
            "the per-namespace listing once per kind and namespace it fell back for; a kind refused outright is not asked for in the next namespace, a kind unreadable this time is",
        )

    def test_a_cut_referent_listing_leaves_references_unresolved(self):
        with patch.object(stall_report, "run_kubectl", return_value=(0, "payments   present\n", "credential proxy output truncated\n")):
            with redirect_stderr(io.StringIO()):
                self.assertIsNone(stall_report.kubectl_names_cluster_wide("configmaps"))
        with patch.object(stall_report, "run_kubectl", return_value=(0, "payments   present\nbilling   cfg\n", "")):
            self.assertEqual(stall_report.kubectl_names_cluster_wide("configmaps"), {("payments", "present"), ("billing", "cfg")})

    def test_the_raw_collections_are_inside_the_brokers_allowlist_and_the_cut_marker_is_the_shims(self):
        import command_policy
        import credential_proxy_client

        for name, (path, kind, api_version) in stall_report.RAW_COLLECTIONS.items():
            with self.subTest(name=name):
                self.assertIn(path, command_policy.KUBECTL_RAW_COLLECTIONS, "a raw read of a path the broker does not list is refused")
                self.assertEqual(path.rsplit("/", 1)[1], name.split(".", 1)[0])
                self.assertEqual(path.split("/")[-2], api_version.split("/")[-1])
        self.assertIn(stall_report.EVENTS_COLLECTION[0], command_policy.KUBECTL_RAW_COLLECTIONS)
        self.assertLessEqual({stall_report.RAW_LIMIT_KEY, stall_report.RAW_CONTINUE_KEY, stall_report.RAW_FIELD_SELECTOR_KEY}, command_policy.KUBECTL_RAW_QUERY_KEYS)
        self.assertNotIn("secrets", stall_report.RAW_COLLECTIONS)
        self.assertEqual(stall_report.PROXY_TRUNCATED_TEXT, credential_proxy_client.OUTPUT_TRUNCATED_MARKER)
        self.assertEqual(stall_report.EXIT_OUTPUT_TRUNCATED, credential_proxy_client.EXIT_OUTPUT_TRUNCATED)
        self.assertNotIn(stall_report.EVENTS_RESOURCE, stall_report.RAW_COLLECTIONS, "events have their own collection, read with the Warning selector")
        for excluded in stall_report.SCAN_EXCLUDED_RESOURCES:
            self.assertNotIn(excluded, stall_report.RAW_COLLECTIONS)

    def test_extra_kinds_are_parsed_per_namespace(self):
        self.assertEqual(stall_report.parse_extra_kinds(["billing=a,b", "billing=c", "web=d"]), {"billing": ["a", "b", "c"], "web": ["d"]})
        with self.assertRaises(ValueError):
            stall_report.parse_extra_kinds(["billing"])

    def test_main_cluster_mode_prints_the_report_and_needs_json(self):
        report = {"namespaces": {NAMESPACE: {"namespace": NAMESPACE, "stalled_resources": 0, "findings": []}}, "fallback": [], "events_fallback": False}
        with patch.object(stall_report, "report_cluster", return_value=report) as mocked:
            with redirect_stdout(io.StringIO()) as out:
                rc = stall_report.main(["--namespaces", f"{NAMESPACE},{SECOND_NAMESPACE}", "--kind", "deployments", "--extra-kinds", f"{SECOND_NAMESPACE}=jobs", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue()), report)
        self.assertEqual(mocked.call_args.args[:3], ([NAMESPACE, SECOND_NAMESPACE], "deployments", {SECOND_NAMESPACE: ["jobs"]}))
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                stall_report.main(["--namespaces", NAMESPACE])
            for empty in ("", ","):
                with self.subTest(namespaces=empty), patch.object(stall_report, "report_cluster") as untouched:
                    with self.assertRaises(SystemExit):
                        stall_report.main(["--namespaces", empty, "--json"])
                    untouched.assert_not_called()
            with self.assertRaises(SystemExit):
                stall_report.main(["--namespace", NAMESPACE, "--extra-kinds", "a=b", "--json"])
        with patch.object(stall_report, "report_cluster", side_effect=RuntimeError("kubectl api-resources failed")):
            with redirect_stderr(io.StringIO()):
                self.assertEqual(stall_report.main(["--namespaces", NAMESPACE, "--json"]), 2)


class InProcessShimAndParallelCollection(unittest.TestCase):
    """Tests for in-process credential shim dispatch, parallel collection, and timestamp caching."""

    REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

    def setUp(self):
        stall_report._SHIM_CACHE.clear()
        stall_report._parse_time_str.cache_clear()

    def test_run_kubectl_uses_inprocess_shim_across_all_three_shipped_layouts(self):
        import py_compile
        import shutil
        import tempfile
        import credential_proxy_client

        launcher_src = os.path.join(self.REPO_ROOT, "deploy", "shared", "credential-proxy-exec.py")
        wrapper_src = os.path.join(self.REPO_ROOT, "deploy", "sandbox", "session-kubectl.sh")

        with tempfile.TemporaryDirectory() as tmp:
            # Layout 1: direct copy of credential_proxy_client.py as kubectl on PATH
            bin1 = os.path.join(tmp, "bin1")
            os.makedirs(bin1)
            direct_kubectl = os.path.join(bin1, "kubectl")
            shutil.copyfile(credential_proxy_client.__file__, direct_kubectl)
            os.chmod(direct_kubectl, 0o755)

            # Layout 2: #5806 precompiled credential-proxy-exec launcher symlinked as kubectl
            libexec = os.path.join(tmp, "libexec")
            bin2 = os.path.join(tmp, "bin2")
            os.makedirs(libexec)
            os.makedirs(bin2)
            client_copy = os.path.join(libexec, "credential_proxy_client.py")
            shutil.copyfile(credential_proxy_client.__file__, client_copy)
            py_compile.compile(client_copy, doraise=True)
            launcher_dst = os.path.join(libexec, "credential-proxy-exec")
            shutil.copyfile(launcher_src, launcher_dst)
            os.chmod(launcher_dst, 0o755)
            os.symlink(launcher_dst, os.path.join(bin2, "kubectl"))

            # Layout 3: worker session-kubectl.sh delegating CLIENT to launcher
            bin3 = os.path.join(tmp, "bin3")
            os.makedirs(bin3)
            with open(wrapper_src, "r", encoding="utf-8") as handle:
                wrapper_text = handle.read().replace(
                    "readonly CLIENT=/opt/credential-proxy/libexec/credential-proxy-exec",
                    f"readonly CLIENT={launcher_dst}",
                )
            wrapper_kubectl = os.path.join(bin3, "kubectl")
            with open(wrapper_kubectl, "w", encoding="utf-8") as handle:
                handle.write(wrapper_text)
            os.chmod(wrapper_kubectl, 0o755)

            for name, bin_dir in (("direct", bin1), ("launcher", bin2), ("session-kubectl", bin3)):
                with self.subTest(layout=name):
                    stall_report._SHIM_CACHE.clear()
                    env = {
                        "CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765",
                        "CREDENTIAL_PROXY_EXEC_URL": "",
                        "KUBE_PROXY_URL": "",
                        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                    }
                    with patch.dict(os.environ, env, clear=False), \
                         patch.object(
                             credential_proxy_client,
                             "execute_captured",
                             return_value=(0, '{"items":[]}\n', ""),
                         ) as exec_cap, \
                         patch.object(stall_report, "_ORIG_SUBPROCESS_RUN", stall_report.subprocess.run):
                        rc, out, err = stall_report.run_kubectl(["get", "pods", "-o", "json"])
                    self.assertEqual((0, '{"items":[]}\n', ""), (rc, out, err))
                    exec_cap.assert_called_once_with(
                        "http://127.0.0.1:8765", ["kubectl", "get", "pods", "-o", "json"], fallback=None
                    )

            # Layout 3 with KUBE_PROXY_URL set must NOT bypass session-kubectl.sh -> returns None
            stall_report._SHIM_CACHE.clear()
            env_kube_proxy = {
                "CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765",
                "CREDENTIAL_PROXY_EXEC_URL": "",
                "KUBE_PROXY_URL": "https://k8s-api-proxy.local:8443",
                "PATH": f"{bin3}:{os.environ.get('PATH', '')}",
            }
            with patch.dict(os.environ, env_kube_proxy, clear=False), \
                 patch.object(stall_report, "_ORIG_SUBPROCESS_RUN", stall_report.subprocess.run):
                self.assertIsNone(stall_report._shim_client())

    def test_run_kubectl_supports_exec_tier_routing_and_captures_malformed_exec_url_notice(self):
        import shutil
        import tempfile
        import credential_proxy_client

        with tempfile.TemporaryDirectory() as tmp:
            shim_path = os.path.join(tmp, "kubectl")
            shutil.copyfile(credential_proxy_client.__file__, shim_path)
            os.chmod(shim_path, 0o755)

            # 1. Both CREDENTIAL_PROXY_EXEC_URL and CREDENTIAL_PROXY_URL -> primary + fallback
            env_both = {
                "CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765",
                "CREDENTIAL_PROXY_EXEC_URL": "http://127.0.0.1:8766",
                "KUBE_PROXY_URL": "",
                "PATH": f"{tmp}:{os.environ.get('PATH', '')}",
            }
            with patch.dict(os.environ, env_both, clear=False), \
                 patch.object(
                     credential_proxy_client,
                     "execute_captured",
                     return_value=(0, '{"items":[]}\n', ""),
                 ) as exec_cap, \
                 patch.object(stall_report, "_ORIG_SUBPROCESS_RUN", stall_report.subprocess.run):
                rc, out, err = stall_report.run_kubectl(["get", "pods"])
            self.assertEqual((0, '{"items":[]}\n', ""), (rc, out, err))
            exec_cap.assert_called_once_with(
                "http://127.0.0.1:8766",
                ["kubectl", "get", "pods"],
                fallback="http://127.0.0.1:8765",
            )

            # 2. CREDENTIAL_PROXY_EXEC_URL alone (no stateful URL) -> dispatches in-process
            env_exec_only = {
                "CREDENTIAL_PROXY_URL": "",
                "CREDENTIAL_PROXY_EXEC_URL": "http://127.0.0.1:8766",
                "KUBE_PROXY_URL": "",
                "PATH": f"{tmp}:{os.environ.get('PATH', '')}",
            }
            with patch.dict(os.environ, env_exec_only, clear=False), \
                 patch.object(
                     credential_proxy_client,
                     "execute_captured",
                     return_value=(0, "ok\n", ""),
                 ) as exec_cap, \
                 patch.object(stall_report, "_ORIG_SUBPROCESS_RUN", stall_report.subprocess.run):
                rc, out, err = stall_report.run_kubectl(["get", "pods"])
            self.assertEqual((0, "ok\n", ""), (rc, out, err))
            exec_cap.assert_called_once_with(
                "http://127.0.0.1:8766", ["kubectl", "get", "pods"], fallback=None
            )

            # 3. Malformed CREDENTIAL_PROXY_EXEC_URL notice captured in returned stderr, not sys.stderr
            saved_flag = credential_proxy_client._exec_url_notice_written
            credential_proxy_client._exec_url_notice_written = False
            proc_err = io.StringIO()
            env_bad_exec = {
                "CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765",
                "CREDENTIAL_PROXY_EXEC_URL": "https://bad-scheme.invalid:8766",
                "KUBE_PROXY_URL": "",
                "PATH": f"{tmp}:{os.environ.get('PATH', '')}",
            }
            try:
                with patch.dict(os.environ, env_bad_exec, clear=False), \
                     patch.object(
                         credential_proxy_client,
                         "execute_captured",
                         return_value=(0, "ok\n", "cmd-warn\n"),
                     ) as exec_cap, \
                     patch.object(stall_report, "_ORIG_SUBPROCESS_RUN", stall_report.subprocess.run), \
                     redirect_stderr(proc_err):
                    rc, out, err = stall_report.run_kubectl(["get", "pods"])
            finally:
                credential_proxy_client._exec_url_notice_written = saved_flag
            self.assertEqual("", proc_err.getvalue())
            self.assertEqual(0, rc)
            self.assertEqual("ok\n", out)
            self.assertIn("CREDENTIAL_PROXY_EXEC_URL", err)
            self.assertTrue(err.endswith("cmd-warn\n"))
            exec_cap.assert_called_once_with(
                "http://127.0.0.1:8765", ["kubectl", "get", "pods"], fallback=None
            )

    def test_run_kubectl_falls_back_to_subprocess_when_monkeypatched_unset_or_modified_wrapper(self):
        import shutil
        import tempfile
        import credential_proxy_client

        launcher_src = os.path.join(self.REPO_ROOT, "deploy", "shared", "credential-proxy-exec.py")
        wrapper_src = os.path.join(self.REPO_ROOT, "deploy", "sandbox", "session-kubectl.sh")

        with tempfile.TemporaryDirectory() as tmp:
            shim_path = os.path.join(tmp, "kubectl")
            shutil.copyfile(credential_proxy_client.__file__, shim_path)
            os.chmod(shim_path, 0o755)

            fake_completed = stall_report.subprocess.CompletedProcess(
                ["kubectl", "get", "pods"], 0, stdout="sub-out", stderr=""
            )
            # 1. Both broker URLs unset -> subprocess.run
            env_no_url = {
                "CREDENTIAL_PROXY_URL": "",
                "CREDENTIAL_PROXY_EXEC_URL": "",
                "PATH": f"{tmp}:{os.environ.get('PATH', '')}",
            }
            with patch.dict(os.environ, env_no_url, clear=False), \
                 patch.object(stall_report.subprocess, "run", return_value=fake_completed) as sub_run, \
                 patch.object(credential_proxy_client, "execute_captured") as exec_cap:
                rc, out, err = stall_report.run_kubectl(["get", "pods"])
                self.assertEqual((0, "sub-out", ""), (rc, out, err))
                sub_run.assert_called_once()
                exec_cap.assert_not_called()

            # 2. CREDENTIAL_PROXY_URL set, but subprocess.run monkeypatched -> subprocess.run
            env_with_url = {
                "CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765",
                "CREDENTIAL_PROXY_EXEC_URL": "",
                "KUBE_PROXY_URL": "",
                "PATH": f"{tmp}:{os.environ.get('PATH', '')}",
            }
            with patch.dict(os.environ, env_with_url, clear=False), \
                 patch.object(stall_report.subprocess, "run", return_value=fake_completed) as sub_run, \
                 patch.object(credential_proxy_client, "execute_captured") as exec_cap:
                rc, out, err = stall_report.run_kubectl(["get", "pods"])
                self.assertEqual((0, "sub-out", ""), (rc, out, err))
                sub_run.assert_called_once()
                exec_cap.assert_not_called()

            # 3. Modified launcher body fails closed -> None
            with open(launcher_src, "r", encoding="utf-8") as handle:
                modified_launcher = handle.read() + "\nprint('extra')\n"
            with open(shim_path, "w", encoding="utf-8") as handle:
                handle.write(modified_launcher)
            stall_report._SHIM_CACHE.clear()
            with patch.dict(os.environ, env_with_url, clear=False):
                self.assertIsNone(stall_report._shim_client())

            # 4. Modified session-kubectl.sh (extra top-level statement or .. in CLIENT) fails closed
            with open(wrapper_src, "r", encoding="utf-8") as handle:
                base_wrapper = handle.read()
            for bad_wrapper in (
                base_wrapper.replace("#!/bin/sh\n", "#!/bin/sh\necho side-effect\n", 1),
                base_wrapper.replace(
                    "readonly CLIENT=/opt/credential-proxy/libexec/credential-proxy-exec",
                    "readonly CLIENT=/opt/../etc/credential-proxy-exec",
                ),
                base_wrapper + "\necho trailing\n",
            ):
                with open(shim_path, "w", encoding="utf-8") as handle:
                    handle.write(bad_wrapper)
                stall_report._SHIM_CACHE.clear()
                with patch.dict(os.environ, env_with_url, clear=False):
                    self.assertIsNone(stall_report._shim_client())

    def test_shim_client_size_guard_cache_invalidation_standalone_fallback_and_thread_safety(self):
        import py_compile
        import shutil
        import tempfile
        import threading
        import credential_proxy_client

        launcher_src = os.path.join(self.REPO_ROOT, "deploy", "shared", "credential-proxy-exec.py")

        with tempfile.TemporaryDirectory() as tmp:
            shim_path = os.path.join(tmp, "kubectl")

            # 1. File larger than _MAX_SHIM_BYTES is rejected via stat without open() or os.open()
            with open(shim_path, "wb") as handle:
                handle.write(b"x" * (stall_report._MAX_SHIM_BYTES + 10))
            os.chmod(shim_path, 0o755)
            stall_report._SHIM_CACHE.clear()
            with patch("builtins.open", side_effect=AssertionError("open() should not be called on oversized file")), \
                 patch("os.open", side_effect=AssertionError("os.open() should not be called on oversized file")):
                self.assertIsNone(stall_report._load_shim_client(shim_path))

            # 2. Cache invalidation when the target file behind a launcher changes on disk
            libexec = os.path.join(tmp, "libexec")
            bin_dir = os.path.join(tmp, "bin")
            os.makedirs(libexec)
            os.makedirs(bin_dir)
            client_copy = os.path.join(libexec, "credential_proxy_client.py")
            shutil.copyfile(credential_proxy_client.__file__, client_copy)
            py_compile.compile(client_copy, doraise=True)
            launcher_dst = os.path.join(libexec, "credential-proxy-exec")
            shutil.copyfile(launcher_src, launcher_dst)
            os.chmod(launcher_dst, 0o755)
            os.symlink(launcher_dst, os.path.join(bin_dir, "kubectl"))

            env = {
                "CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765",
                "CREDENTIAL_PROXY_EXEC_URL": "",
                "KUBE_PROXY_URL": "",
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            }
            stall_report._SHIM_CACHE.clear()
            with patch.dict(os.environ, env, clear=False), \
                 patch.object(stall_report, "_ORIG_SUBPROCESS_RUN", stall_report.subprocess.run):
                self.assertIs(stall_report._shim_client(), credential_proxy_client)
                # Mutate the target behind the launcher; watched-file check must invalidate cache
                with open(client_copy, "wb") as handle:
                    handle.write(b"#!/usr/bin/env python3\n# replaced\n")
                self.assertIsNone(stall_report._shim_client())

            # Restore client_copy for standalone fallback and concurrency checks
            shutil.copyfile(credential_proxy_client.__file__, client_copy)
            py_compile.compile(client_copy, doraise=True)

            # 3. Standalone fallback loads credential_proxy_client from launcher target when not on sys.path
            stall_report._SHIM_CACHE.clear()
            saved_mod = sys.modules.pop("credential_proxy_client", None)
            try:
                with patch.object(sys, "path", []):
                    loaded = stall_report._load_shim_client(os.path.join(bin_dir, "kubectl"))
                    self.assertIsNotNone(loaded)
                    self.assertTrue(hasattr(loaded, "execute_captured"))
                    self.assertTrue(hasattr(loaded, "exec_endpoints"))
            finally:
                if saved_mod is not None:
                    sys.modules["credential_proxy_client"] = saved_mod

            # 4. Concurrent cold-cache calls to _shim_client() across 8 threads
            stall_report._SHIM_CACHE.clear()
            barrier = threading.Barrier(8, timeout=5.0)
            results = [None] * 8

            def worker(idx):
                barrier.wait()
                results[idx] = stall_report._shim_client()

            with patch.dict(os.environ, env, clear=False), \
                 patch.object(stall_report, "_ORIG_SUBPROCESS_RUN", stall_report.subprocess.run):
                threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=5.0)
            for res in results:
                self.assertIs(res, credential_proxy_client)

    def test_parallel_reads_and_parallel_cluster_reads_respect_monkeypatching(self):
        self.assertTrue(stall_report._parallel_reads())
        self.assertTrue(stall_report._parallel_cluster_reads())
        with patch.object(stall_report, "KUBECTL", "/custom/kubectl"):
            self.assertFalse(stall_report._parallel_reads())
            self.assertFalse(stall_report._parallel_cluster_reads())
            self.assertIsNone(stall_report._shim_client())
        for name in ("run_kubectl", "kubectl_json", "kubectl_names"):
            with patch.object(stall_report, name, side_effect=getattr(stall_report, name)):
                self.assertFalse(stall_report._parallel_reads(), name)
                self.assertFalse(stall_report._parallel_cluster_reads(), name)
        with patch.object(stall_report.subprocess, "run", side_effect=stall_report.subprocess.run):
            self.assertFalse(stall_report._parallel_reads())
            self.assertFalse(stall_report._parallel_cluster_reads())
        for name in (
            "warn_lines",
            "raw_pages",
            "chunked_listing",
            "read_kind_cluster_wide",
            "read_warning_events_cluster_wide",
        ):
            with patch.object(stall_report, name, side_effect=getattr(stall_report, name)):
                self.assertTrue(stall_report._parallel_reads(), name)
                self.assertFalse(stall_report._parallel_cluster_reads(), name)

    def test_parallel_collect_runs_concurrently_and_preserves_deterministic_order(self):
        import threading
        import time

        # 4 resources + 1 events call = 5 concurrent calls (<= MAX_COLLECT_WORKERS=6)
        barrier = threading.Barrier(5, timeout=5.0)

        def concurrent_kubectl(args):
            barrier.wait()
            kind = args[1]
            # Deliberately stagger completion in reverse order so earlier resources finish last
            if kind == "deployments":
                time.sleep(0.03)
                return 0, '{"items":[{"kind":"Deployment","metadata":{"name":"d1"}}]}', "dep-warn\n"
            if kind == "pods":
                time.sleep(0.02)
                return 1, "", "pods-boom"
            if kind == "statefulsets":
                time.sleep(0.01)
                return 0, '{"items":[{"kind":"StatefulSet","metadata":{"name":"s1"}}]}', "sts-warn\n"
            if kind == "gateways":
                return 0, '{"items":[{"kind":"Gateway","metadata":{"name":"g1"}}]}', "gw-warn\n"
            if kind == "events":
                return 0, '{"items":[{"kind":"Event","metadata":{"name":"e1"}}]}', "ev-warn\n"
            raise AssertionError(f"unexpected args: {args}")

        err_buf = io.StringIO()
        with patch.object(stall_report, "run_kubectl", side_effect=concurrent_kubectl), \
             patch.object(stall_report, "_parallel_reads", return_value=True), \
             redirect_stderr(err_buf):
            objects, events = stall_report.collect(
                NAMESPACE,
                "deployments,pods,statefulsets,gateways",
                workers=stall_report.MAX_COLLECT_WORKERS,
            )

        self.assertEqual(
            [(o["kind"], o["metadata"]["name"]) for o in objects],
            [("Deployment", "d1"), ("StatefulSet", "s1"), ("Gateway", "g1")],
        )
        self.assertEqual([e["metadata"]["name"] for e in events], ["e1"])

        stderr_lines = [line.strip() for line in err_buf.getvalue().splitlines() if line.strip()]
        self.assertEqual(
            stderr_lines,
            [
                "warning: dep-warn",
                "warning: pods-boom",
                f"warning: pods in {NAMESPACE} not scanned; its objects are missing from the count: kubectl get pods -n {NAMESPACE} --show-managed-fields failed (1): pods-boom",
                "warning: sts-warn",
                "warning: gw-warn",
                "warning: ev-warn",
            ],
        )

    def test_parallel_collect_total_failure_raises_without_emitting_events_warning(self):
        def failing_kubectl(args):
            kind = args[1]
            if kind == "events":
                return 1, "", "events-should-not-be-warned"
            return 1, "", f"{kind}-failed"

        err_buf = io.StringIO()
        with patch.object(stall_report, "run_kubectl", side_effect=failing_kubectl), \
             patch.object(stall_report, "_parallel_reads", return_value=True), \
             redirect_stderr(err_buf):
            with self.assertRaises(RuntimeError) as ctx:
                stall_report.collect(NAMESPACE, "deployments,pods")

        self.assertIn(f"no kind could be read in {NAMESPACE}: deployments, pods", str(ctx.exception))
        self.assertNotIn("events-should-not-be-warned", err_buf.getvalue())
        self.assertNotIn("events in", err_buf.getvalue())

    def test_parallel_prefetch_references_preserves_order_refused_sentinel_and_skips_cluster_resolver(self):
        import threading
        import time

        gw = obj(
            "Gateway",
            "edge",
            spec={
                "listeners": [
                    {"name": "https", "tls": {"certificateRefs": [{"kind": "Secret", "name": "edge-tls"}]}},
                ]
            },
            status={},
            managed_fields=[managed(45)],
        )
        dep = healthy_deployment("web")
        dep["spec"]["template"]["spec"]["containers"][0]["envFrom"] = [
            {"configMapRef": {"name": "web-cm"}},
        ]
        dep["spec"]["template"]["spec"]["volumes"] = [
            {"name": "pvc", "persistentVolumeClaim": {"claimName": "web-pvc"}},
        ]
        dep["metadata"]["managedFields"] = [managed(45)]

        barrier = threading.Barrier(3, timeout=5.0)

        def concurrent_names_kubectl(args):
            barrier.wait()
            resource = args[1]
            if resource == "secrets":
                time.sleep(0.03)
                return 1, "", "Error from server (Forbidden): secrets is forbidden"
            if resource == "configmaps":
                time.sleep(0.01)
                return 0, "configmap/web-cm\n", ""
            if resource == "persistentvolumeclaims":
                return 1, "", "transient-503"
            raise AssertionError(f"unexpected args: {args}")

        resolver = stall_report.NameResolver()
        err_buf = io.StringIO()
        with patch.object(stall_report, "run_kubectl", side_effect=concurrent_names_kubectl) as mocked_run, \
             patch.object(stall_report, "_parallel_reads", return_value=True), \
             redirect_stderr(err_buf):
            stall_report.prefetch_references(
                resolver, [gw, dep], NOW, workers=stall_report.MAX_COLLECT_WORKERS
            )
            self.assertEqual(mocked_run.call_count, 3)
            findings = stall_report.analyze([gw, dep], [], resolver, NOW)
            self.assertEqual(mocked_run.call_count, 3)

        self.assertEqual(findings, [])
        self.assertIsInstance(resolver._cache[("secrets", NAMESPACE)], stall_report.ListingRefused)
        self.assertEqual(resolver._cache[("configmaps", NAMESPACE)], {"web-cm"})
        self.assertIsNone(resolver._cache[("persistentvolumeclaims", NAMESPACE)])

        stderr_lines = [line.strip() for line in err_buf.getvalue().splitlines() if line.strip()]
        self.assertEqual(
            stderr_lines,
            [
                f"warning: cannot list secrets in {NAMESPACE}; references to secrets are not checked: Error from server (Forbidden): secrets is forbidden",
                f"warning: cannot list persistentvolumeclaims in {NAMESPACE}; references to persistentvolumeclaims are not checked: transient-503",
            ],
        )

        # ClusterNameResolver is skipped by prefetch_references (type(resolver) is not NameResolver)
        cluster_resolver = stall_report.ClusterNameResolver()
        with patch.object(stall_report, "run_kubectl") as untouched, \
             patch.object(stall_report, "_parallel_reads", return_value=True):
            stall_report.prefetch_references(cluster_resolver, [gw, dep], NOW)
            untouched.assert_not_called()

        # 0-key and 1-key paths
        single_resolver = stall_report.NameResolver()
        with patch.object(stall_report, "run_kubectl") as untouched, \
             patch.object(stall_report, "_parallel_reads", return_value=True):
            stall_report.prefetch_references(single_resolver, [healthy_deployment("clean")], NOW)
            untouched.assert_not_called()

        with patch.object(
            stall_report, "run_kubectl", return_value=(0, "secret/edge-tls\n", "")
        ) as one_call, patch.object(stall_report, "_parallel_reads", return_value=True):
            stall_report.prefetch_references(single_resolver, [gw], NOW)
            one_call.assert_called_once_with(["get", "secrets", "-n", NAMESPACE, "-o", "name"])
        self.assertEqual(single_resolver._cache[("secrets", NAMESPACE)], {"edge-tls"})

    def test_parallel_collect_cluster_runs_concurrently_and_preserves_deterministic_warnings_and_fallback(self):
        import threading
        import time

        # 3 network-reading kinds (deployments raw, pods raw-fail, gateways chunked-fail) + events raw = 4 concurrent calls;
        # deployments/single is a kind/name that returns None without calling kubectl.
        barrier = threading.Barrier(4, timeout=5.0)

        def concurrent_cluster_kubectl(args):
            barrier.wait()
            if "--raw" in args:
                raw_url = args[args.index("--raw") + 1]
                if raw_url.startswith("/apis/apps/v1/deployments"):
                    time.sleep(0.03)
                    return 0, json.dumps({"items": [{"metadata": {"name": "web", "namespace": NAMESPACE}}], "metadata": {}}), ""
                if raw_url.startswith("/api/v1/pods"):
                    time.sleep(0.02)
                    return 1, "", "pods-raw-err\n"
                if raw_url.startswith("/api/v1/events"):
                    return 0, json.dumps({"items": [{"metadata": {"name": "ev1", "namespace": NAMESPACE}}], "metadata": {}}), ""
            if args[1] == GATEWAYS:
                time.sleep(0.01)
                return 1, "", "gateways-chunk-err\n"
            raise AssertionError(f"unexpected args: {args}")

        err_buf = io.StringIO()
        with patch.object(stall_report, "run_kubectl", side_effect=concurrent_cluster_kubectl), \
             patch.object(stall_report, "_parallel_cluster_reads", return_value=True), \
             redirect_stderr(err_buf):
            objects, events, fallback = stall_report.collect_cluster(
                ["deployments", "pods", GATEWAYS, "deployments/single"],
                workers=stall_report.MAX_COLLECT_WORKERS,
            )

        self.assertEqual(list(objects.keys()), ["deployments"])
        self.assertEqual([o["metadata"]["name"] for o in objects["deployments"][NAMESPACE]], ["web"])
        self.assertEqual([e["metadata"]["name"] for e in events[NAMESPACE]], ["ev1"])
        self.assertEqual(fallback, ["pods", GATEWAYS, "deployments/single"])

        stderr_lines = [line.strip() for line in err_buf.getvalue().splitlines() if line.strip()]
        self.assertEqual(
            stderr_lines,
            [
                "warning: pods-raw-err",
                "warning: /api/v1/pods page failed (1); read per namespace instead",
                "warning: gateways-chunk-err",
                f"warning: {GATEWAYS} across every namespace failed (1); read per namespace instead",
            ],
        )

    def test_late_binding_and_monkeypatch_guards_for_resolvers(self):
        # 1. NameResolver() with no args picks up post-import patch on kubectl_names
        with patch.object(stall_report, "kubectl_names", return_value={"patched-secret"}) as patched_names:
            resolver = stall_report.NameResolver()
            self.assertTrue(resolver.exists("Secret", NAMESPACE, "patched-secret"))
            patched_names.assert_called_once_with("secrets", NAMESPACE)

        # 2. ScaleResolver() with no args picks up post-import patch on kubectl_json
        with patch.object(stall_report, "kubectl_json", return_value={"items": []}) as patched_json:
            scale = stall_report.ScaleResolver()
            self.assertEqual(scale._items("get", "deployments"), [])
            patched_json.assert_called_once_with(["get", "deployments"])

        # 3. ClusterNameResolver() with no args picks up post-import patch on kubectl_names_cluster_wide
        with patch.object(
            stall_report, "kubectl_names_cluster_wide", return_value={(NAMESPACE, "cm1")}
        ) as patched_cluster:
            cluster_resolver = stall_report.ClusterNameResolver()
            self.assertTrue(cluster_resolver.exists("ConfigMap", NAMESPACE, "cm1"))
            patched_cluster.assert_called_once_with("configmaps")

        # 4. Patching NameResolver.exists disables prefetch_references
        gw = obj(
            "Gateway",
            "edge",
            spec={
                "listeners": [
                    {"name": "https", "tls": {"certificateRefs": [{"kind": "Secret", "name": "edge-tls"}]}},
                ]
            },
            status={},
            managed_fields=[managed(45)],
        )
        resolver = stall_report.NameResolver()
        with patch.object(stall_report.NameResolver, "exists", return_value=True), \
             patch.object(stall_report, "run_kubectl") as mocked_run:
            stall_report.prefetch_references(resolver, [gw], NOW)
            mocked_run.assert_not_called()

    def test_parse_kubectl_json_rejects_non_dict_json(self):
        for payload in ("[1, 2, 3]", "null", '"string"', "42"):
            with self.subTest(payload=payload):
                with self.assertRaises(RuntimeError) as ctx:
                    stall_report._parse_kubectl_json(["get", "pods"], 0, payload, "")
                self.assertIn("returned non-object JSON", str(ctx.exception))

    def test_parse_time_str_lru_cache_hits_and_edge_cases(self):
        stall_report._parse_time_str.cache_clear()
        ts = "2026-09-09T12:00:00Z"
        first = stall_report.parse_time(ts)
        second = stall_report.parse_time(ts)
        self.assertEqual(first, NOW)
        self.assertIs(first, second)
        info = stall_report._parse_time_str.cache_info()
        self.assertEqual(info.hits, 1)
        self.assertEqual(info.misses, 1)

        self.assertIsNone(stall_report.parse_time(""))
        self.assertIsNone(stall_report.parse_time(None))
        self.assertIsNone(stall_report.parse_time(12345))
        self.assertIsNone(stall_report._parse_time_str(""))

        self.assertIsNone(stall_report.parse_time("not-a-timestamp"))
        self.assertIsNone(stall_report.parse_time("not-a-timestamp"))
        self.assertEqual(stall_report._parse_time_str.cache_info().hits, 2)

        naive = stall_report.parse_time("2026-09-09T12:00:00")
        self.assertEqual(naive, NOW)

    def test_session_kubectl_parser_rejects_elif_else_compound_fi_and_whitespace_kube_proxy_url(self):
        import shutil
        import tempfile
        import credential_proxy_client

        launcher_src = os.path.join(self.REPO_ROOT, "deploy", "shared", "credential-proxy-exec.py")
        wrapper_src = os.path.join(self.REPO_ROOT, "deploy", "sandbox", "session-kubectl.sh")
        with open(wrapper_src, "r", encoding="utf-8") as handle:
            base_wrapper = handle.read()

        self.assertEqual(
            stall_report._session_kubectl_client_path(base_wrapper.encode("utf-8")),
            "/opt/credential-proxy/libexec/credential-proxy-exec",
        )
        closing_marker = '\nfi\nif [ -n "${CREDENTIAL_PROXY_URL:-}" ]'
        self.assertIn(closing_marker, base_wrapper)
        adversarial_variants = {
            "elif_at_depth_1": base_wrapper.replace(
                closing_marker,
                '\nelif [ -n "${CUSTOM_MODE:-}" ]; then\n  exit 41' + closing_marker,
                1,
            ),
            "else_block_at_depth_1": base_wrapper.replace(
                closing_marker,
                "\nelse\n  exit 42" + closing_marker,
                1,
            ),
            "else_inline_at_depth_1": base_wrapper.replace(
                closing_marker,
                "\nelse exit 42" + closing_marker,
                1,
            ),
            "fi_semicolon_cmd": base_wrapper.replace(
                closing_marker,
                '\nfi; exit 99\nif [ -n "${CREDENTIAL_PROXY_URL:-}" ]',
                1,
            ),
            "fi_and_cmd": base_wrapper.replace(
                closing_marker,
                '\nfi && exit 99\nif [ -n "${CREDENTIAL_PROXY_URL:-}" ]',
                1,
            ),
            "cmd_then_fi": base_wrapper.replace(
                closing_marker,
                '\ntrue; fi\nif [ -n "${CREDENTIAL_PROXY_URL:-}" ]',
                1,
            ),
            "compound_if": base_wrapper.replace(
                closing_marker,
                '\ntrue; if false; then\n  true\nfi' + closing_marker,
                1,
            ),
            "elif_subshell_paren": base_wrapper.replace(
                closing_marker,
                '\nelif(true); then\n  exit 41' + closing_marker,
                1,
            ),
            "else_subshell_paren": base_wrapper.replace(
                closing_marker,
                "\nelse(exit 42)" + closing_marker,
                1,
            ),
            "fi_subshell_paren": base_wrapper.replace(
                closing_marker,
                '\nfi(exit 99)\nif [ -n "${CREDENTIAL_PROXY_URL:-}" ]',
                1,
            ),
            "if_subshell_paren": base_wrapper.replace(
                closing_marker,
                '\nif(false); then\n  true\nfi' + closing_marker,
                1,
            ),
        }
        for label, bad_text in adversarial_variants.items():
            with self.subTest(variant=label):
                self.assertIsNone(stall_report._session_kubectl_client_path(bad_text.encode("utf-8")))

        # Whitespace-only KUBE_PROXY_URL is non-empty in POSIX sh ([ -n "${KUBE_PROXY_URL:-}" ])
        # and must not bypass session-kubectl.sh.
        with tempfile.TemporaryDirectory() as tmp:
            libexec = os.path.join(tmp, "libexec")
            bin_dir = os.path.join(tmp, "bin")
            os.makedirs(libexec)
            os.makedirs(bin_dir)
            shutil.copyfile(credential_proxy_client.__file__, os.path.join(libexec, "credential_proxy_client.py"))
            launcher_dst = os.path.join(libexec, "credential-proxy-exec")
            shutil.copyfile(launcher_src, launcher_dst)
            os.chmod(launcher_dst, 0o755)
            wrapper_dst = os.path.join(bin_dir, "kubectl")
            with open(wrapper_dst, "w", encoding="utf-8") as handle:
                handle.write(
                    base_wrapper.replace(
                        "readonly CLIENT=/opt/credential-proxy/libexec/credential-proxy-exec",
                        f"readonly CLIENT={launcher_dst}",
                    )
                )
            os.chmod(wrapper_dst, 0o755)

            env_ws = {
                "CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765",
                "CREDENTIAL_PROXY_EXEC_URL": "",
                "KUBE_PROXY_URL": " ",
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            }
            stall_report._SHIM_CACHE.clear()
            with patch.dict(os.environ, env_ws, clear=False), \
                 patch.object(stall_report, "_ORIG_SUBPROCESS_RUN", stall_report.subprocess.run):
                self.assertIsNone(stall_report._shim_client())

    def test_session_kubectl_parser_rejects_multiline_quotes_heredocs_and_continuations(self):
        tail = (
            'if [ -n "${CREDENTIAL_PROXY_URL:-}" ] || [ -n "${CREDENTIAL_PROXY_EXEC_URL:-}" ]; then\n'
            'exec "$CLIENT" kubectl "$@"\n'
            "fi\n"
            'exec "$REAL_KUBECTL" "$@"\n'
        )
        header = (
            "#!/bin/sh\n"
            "readonly CLIENT=/opt/credential-proxy/libexec/credential-proxy-exec\n"
        )
        # Positive check: real shipped wrapper (with valid blank-bounded backslash
        # continuations, single-line ${...}, $((...)), and comments containing quotes/backticks)
        # still parses cleanly.
        wrapper_src = os.path.join(self.REPO_ROOT, "deploy", "sandbox", "session-kubectl.sh")
        with open(wrapper_src, "rb") as handle:
            self.assertEqual(
                stall_report._session_kubectl_client_path(handle.read()),
                "/opt/credential-proxy/libexec/credential-proxy-exec",
            )

        spoofs = {
            "multiline_double_quote": (
                header
                + 'if [ -n "${KUBE_PROXY_URL:-}" ]; then\n'
                + '  echo "a\n'
                + 'if b" >&2\n'
                + "fi\n"
                + "some_top_level_command\n"
                + 'echo "x\n'
                + "fi\n"
                + '#" >/dev/null\n'
                + tail
            ),
            "multiline_double_quote_escaped_trailing_quote": (
                header
                + 'if [ -n "${KUBE_PROXY_URL:-}" ]; then\n'
                + '  echo "a\\"\n'
                + 'if b"\n'
                + "fi\n"
                + "some_top_level_command\n"
                + 'echo "x\\"\n'
                + "fi\n"
                + '#"\n'
                + tail
            ),
            "multiline_single_quote": (
                header
                + 'if [ -n "${KUBE_PROXY_URL:-}" ]; then\n'
                + "  echo 'a\n"
                + "if b' >&2\n"
                + "fi\n"
                + "some_top_level_command\n"
                + "echo 'x\n"
                + "fi\n"
                + "#' >/dev/null\n"
                + tail
            ),
            "heredoc_in_kube_proxy_block": (
                header
                + 'if [ -n "${KUBE_PROXY_URL:-}" ]; then\n'
                + "  cat <<EOF\n"
                + "if [ -n x ]; then\n"
                + "EOF\n"
                + "fi\n"
                + "some_top_level_command\n"
                + "cat <<EOF\n"
                + "fi\n"
                + "EOF\n"
                + tail
            ),
            "heredoc_in_helper_function": (
                header
                + "helper() {\n"
                + "  cat <<EOF\n"
                + "}\n"
                + 'if [ -n "${KUBE_PROXY_URL:-}" ]; then\n'
                + "EOF\n"
                + "}\n"
                + "some_top_level_command\n"
                + "fi\n"
                + tail
            ),
            "multiline_single_quote_in_helper_function": (
                header
                + "helper() {\n"
                + "  x='\n"
                + "}\n"
                + "'\n"
                + "}\n"
                + 'if [ -n "${KUBE_PROXY_URL:-}" ]; then\n'
                + "  true\n"
                + "fi\n"
                + tail
            ),
            "backslash_split_fi": (
                header
                + 'if [ -n "${KUBE_PROXY_URL:-}" ]; then\n'
                + "  true\n"
                + "f\\\n"
                + "i\n"
                + "some_top_level_command\n"
                + "fi\n"
                + tail
            ),
            "backslash_continuation_hiding_if_and_fi": (
                header
                + 'if [ -n "${KUBE_PROXY_URL:-}" ]; then\n'
                + "  echo \\\n"
                + "if [ -n x ]; then\n"
                + "fi\n"
                + "some_top_level_command\n"
                + "echo \\\n"
                + "fi\n"
                + tail
            ),
            "backslash_continuation_inside_double_quote": (
                header
                + 'if [ -n "${KUBE_PROXY_URL:-}" ]; then\n'
                + '  echo "a \\\n'
                + 'if b"\n'
                + "fi\n"
                + tail
            ),
            "backslash_continuation_hiding_func_close": (
                header
                + "helper() {\n"
                + "  echo \\\n"
                + "}\n"
                + "}\n"
                + 'if [ -n "${KUBE_PROXY_URL:-}" ]; then\n'
                + "  true\n"
                + "fi\n"
                + tail
            ),
            "backslash_split_heredoc": (
                header
                + 'if [ -n "${KUBE_PROXY_URL:-}" ]; then\n'
                + "  cat <\\\n"
                + "<EOF\n"
                + "EOF\n"
                + "fi\n"
                + tail
            ),
            "multiline_param_expansion": (
                header
                + 'if [ -n "${KUBE_PROXY_URL:-}" ]; then\n'
                + "  echo ${x:-\n"
                + "if y}\n"
                + "fi\n"
                + tail
            ),
            "command_substitution_in_kube_proxy_block": (
                header
                + 'if [ -n "${KUBE_PROXY_URL:-}" ]; then\n'
                + "  echo $(true)\n"
                + "fi\n"
                + tail
            ),
            "backtick_in_kube_proxy_block": (
                header
                + 'if [ -n "${KUBE_PROXY_URL:-}" ]; then\n'
                + "  echo `true`\n"
                + "fi\n"
                + tail
            ),
            "case_pattern_with_if_keyword": (
                header
                + 'if [ -n "${KUBE_PROXY_URL:-}" ]; then\n'
                + '  case "$x" in\n'
                + "  if )\n"
                + "    true\n"
                + "    ;;\n"
                + "  esac\n"
                + "fi\n"
                + "some_top_level_command\n"
                + "fi\n"
                + tail
            ),
            "trailing_pipe_continuation": (
                header
                + 'if [ -n "${KUBE_PROXY_URL:-}" ]; then\n'
                + '  case "$x" in\n'
                + "  a |\n"
                + "  esac\n"
                + "  )\n"
                + "    ;;\n"
                + "  esac\n"
                + "fi\n"
                + tail
            ),
            "carriage_return_line_desync": (
                header
                + 'if [ -n "${KUBE_PROXY_URL:-}" ]; then\n'
                + "  # comment\rfi\n"
                + "fi\n"
                + tail
            ),
        }
        for label, bad_text in spoofs.items():
            with self.subTest(spoof=label):
                self.assertIsNone(stall_report._session_kubectl_client_path(bad_text.encode("utf-8")))

    def test_shim_client_rejects_fifo_world_writable_inplace_overwrite_and_comment_spoofing(self):
        import shutil
        import tempfile
        import threading
        import credential_proxy_client

        with tempfile.TemporaryDirectory() as tmp:
            # 1. FIFO (direct and simulated TOCTOU past pre-open stat) returns None without blocking
            fifo_path = os.path.join(tmp, "kubectl")
            os.mkfifo(fifo_path)
            result_box: list[object] = ["pending"]

            def probe_fifo():
                direct = stall_report._read_bounded(fifo_path)
                # Simulate TOCTOU: pass a regular-file stat_result while opening the FIFO
                reg_file = os.path.join(tmp, "regular.py")
                with open(reg_file, "wb") as handle:
                    handle.write(b"ok\n")
                fake_reg_info = os.stat(reg_file)
                toctou = stall_report._read_bounded(fifo_path, fake_reg_info)
                result_box[0] = (direct, toctou)

            t = threading.Thread(target=probe_fifo, daemon=True)
            t.start()
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive(), "_read_bounded blocked on FIFO")
            self.assertEqual(result_box[0], (None, None))
            os.unlink(fifo_path)

            # 2. World-writable shim file is rejected
            shim_path = os.path.join(tmp, "kubectl")
            shutil.copyfile(credential_proxy_client.__file__, shim_path)
            os.chmod(shim_path, 0o777)
            env = {
                "CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765",
                "CREDENTIAL_PROXY_EXEC_URL": "",
                "KUBE_PROXY_URL": "",
                "PATH": f"{tmp}:{os.environ.get('PATH', '')}",
            }
            stall_report._SHIM_CACHE.clear()
            with patch.dict(os.environ, env, clear=False), \
                 patch.object(stall_report, "_ORIG_SUBPROCESS_RUN", stall_report.subprocess.run):
                self.assertIsNone(stall_report._shim_client())
            os.chmod(shim_path, 0o755)

            # 3. In-place overwrite (same inode) of an already-loaded standalone module is rejected
            #    both for non-shim content and for altered content that keeps _SHIM_HEADER.
            saved_mod = sys.modules.pop("credential_proxy_client", None)
            try:
                stall_report._SHIM_CACHE.clear()
                with patch.dict(os.environ, env, clear=False), \
                     patch.object(sys, "path", []), \
                     patch.object(stall_report, "_ORIG_SUBPROCESS_RUN", stall_report.subprocess.run):
                    loaded = stall_report._shim_client()
                    self.assertIsNotNone(loaded)
                    orig_ino = os.stat(shim_path).st_ino

                    # Overwrite in place with non-shim text (same inode)
                    with open(shim_path, "wb") as handle:
                        handle.write(b"#!/bin/sh\necho hi\n")
                    self.assertEqual(os.stat(shim_path).st_ino, orig_ino)
                    self.assertIsNone(stall_report._shim_client())

                    # Overwrite in place with different Python that keeps _SHIM_HEADER (same inode)
                    with open(shim_path, "wb") as handle:
                        handle.write(
                            stall_report._SHIM_HEADER
                            + b"\ndef execute_captured(*a, **k): return (0, '', '')\n"
                            + b"def exec_endpoints(*a, **k): return (None, None)\n"
                        )
                    self.assertEqual(os.stat(shim_path).st_ino, orig_ino)
                    self.assertIsNone(stall_report._shim_client())
            finally:
                sys.modules.pop("credential_proxy_client", None)
                if saved_mod is not None:
                    sys.modules["credential_proxy_client"] = saved_mod

            # 4. Comment-only markers must fail AST check without executing top-level code
            sentinel = os.path.join(tmp, "pwned.txt")
            spoof_path = os.path.join(tmp, "spoof_kubectl")
            with open(spoof_path, "wb") as handle:
                handle.write(
                    stall_report._SHIM_HEADER
                    + b"# def execute_captured( def exec_endpoints(\n"
                    + f"open({sentinel!r}, 'w').write('executed')\n".encode("utf-8")
                )
            os.chmod(spoof_path, 0o755)
            saved_mod = sys.modules.pop("credential_proxy_client", None)
            try:
                stall_report._SHIM_CACHE.clear()
                with patch.object(sys, "path", []):
                    self.assertIsNone(stall_report._load_shim_client(spoof_path))
                self.assertFalse(os.path.exists(sentinel), "AST guard allowed top-level execution")
            finally:
                sys.modules.pop("credential_proxy_client", None)
                if saved_mod is not None:
                    sys.modules["credential_proxy_client"] = saved_mod

            # 5. Forged UNCHECKED_HASH .pyc alongside a symlinked shim is never executed
            import importlib.util
            import py_compile

            pyc_dir = os.path.join(tmp, "pyc_attack")
            os.makedirs(pyc_dir)
            real_client = os.path.join(tmp, "real_client.py")
            shutil.copyfile(credential_proxy_client.__file__, real_client)
            os.chmod(real_client, 0o644)
            symlink_kubectl = os.path.join(pyc_dir, "kubectl")
            os.symlink(real_client, symlink_kubectl)

            pyc_sentinel = os.path.join(tmp, "pyc_pwned.txt")
            poison_src = os.path.join(tmp, "poison.py")
            with open(poison_src, "w", encoding="utf-8") as handle:
                handle.write(
                    f"open({pyc_sentinel!r}, 'w').write('pyc')\n"
                    "def execute_captured(*a, **k): return (0, '', '')\n"
                    "def exec_endpoints(*a, **k): return (None, None)\n"
                )
            forged_pyc = importlib.util.cache_from_source(symlink_kubectl)
            os.makedirs(os.path.dirname(forged_pyc), exist_ok=True)
            py_compile.compile(
                poison_src,
                cfile=forged_pyc,
                doraise=True,
                invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
            )
            saved_mod = sys.modules.pop("credential_proxy_client", None)
            try:
                stall_report._SHIM_CACHE.clear()
                with patch.object(sys, "path", []):
                    loaded = stall_report._load_shim_client(symlink_kubectl)
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded.__file__, os.path.realpath(real_client))
                self.assertFalse(os.path.exists(pyc_sentinel), "forged .pyc was executed")
            finally:
                sys.modules.pop("credential_proxy_client", None)
                if saved_mod is not None:
                    sys.modules["credential_proxy_client"] = saved_mod

    def test_shim_client_single_load_cold_start_and_cache_invalidation_edge_cases(self):
        import shutil
        import tempfile
        import types
        import credential_proxy_client

        launcher_src = os.path.join(self.REPO_ROOT, "deploy", "shared", "credential-proxy-exec.py")

        with tempfile.TemporaryDirectory() as tmp:
            # 1. Cold start loads the standalone module ONCE across 3 calls (no Call 2 double-load)
            bin_cold = os.path.join(tmp, "bin_cold")
            os.makedirs(bin_cold)
            direct_kubectl = os.path.join(bin_cold, "kubectl")
            shutil.copyfile(credential_proxy_client.__file__, direct_kubectl)
            os.chmod(direct_kubectl, 0o755)

            env_cold = {
                "CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765",
                "CREDENTIAL_PROXY_EXEC_URL": "",
                "KUBE_PROXY_URL": "",
                "PATH": f"{bin_cold}:{os.environ.get('PATH', '')}",
            }
            saved_mod = sys.modules.pop("credential_proxy_client", None)
            try:
                stall_report._SHIM_CACHE.clear()
                with patch.dict(os.environ, env_cold, clear=False), \
                     patch.object(sys, "path", []), \
                     patch.object(stall_report, "_ORIG_SUBPROCESS_RUN", stall_report.subprocess.run), \
                     patch.object(
                         stall_report,
                         "_load_shim_client_with_watch",
                         wraps=stall_report._load_shim_client_with_watch,
                     ) as spy_load:
                    m1 = stall_report._shim_client()
                    m2 = stall_report._shim_client()
                    m3 = stall_report._shim_client()
                self.assertIsNotNone(m1)
                self.assertIs(m1, m2)
                self.assertIs(m2, m3)
                self.assertEqual(spy_load.call_count, 1)

                # 2. Replacing sys.modules["credential_proxy_client"] with an in-memory module
                #    (__file__=None) must invalidate the cache rather than colliding with Call 1.
                sys.modules["credential_proxy_client"] = types.ModuleType("credential_proxy_client")
                with patch.dict(os.environ, env_cold, clear=False), \
                     patch.object(sys, "path", []), \
                     patch.object(stall_report, "_ORIG_SUBPROCESS_RUN", stall_report.subprocess.run):
                    self.assertIsNone(stall_report._shim_client())
            finally:
                sys.modules.pop("credential_proxy_client", None)
                if saved_mod is not None:
                    sys.modules["credential_proxy_client"] = saved_mod

            # 3. Repointing kubectl symlink to a hard-linked launcher in a different directory
            #    (same st_dev/st_ino/st_size/st_mtime_ns) must invalidate cache via realpath.
            dir_a = os.path.join(tmp, "dir_a")
            dir_b = os.path.join(tmp, "dir_b")
            bin_link = os.path.join(tmp, "bin_link")
            os.makedirs(dir_a)
            os.makedirs(dir_b)
            os.makedirs(bin_link)
            shutil.copyfile(credential_proxy_client.__file__, os.path.join(dir_a, "credential_proxy_client.py"))
            with open(os.path.join(dir_b, "credential_proxy_client.py"), "wb") as handle:
                handle.write(b"#!/usr/bin/env python3\n# different client\n")
            launcher_a = os.path.join(dir_a, "credential-proxy-exec")
            launcher_b = os.path.join(dir_b, "credential-proxy-exec")
            shutil.copyfile(launcher_src, launcher_a)
            os.chmod(launcher_a, 0o755)
            os.link(launcher_a, launcher_b)
            self.assertEqual(os.stat(launcher_a).st_ino, os.stat(launcher_b).st_ino)

            kubectl_link = os.path.join(bin_link, "kubectl")
            os.symlink(launcher_a, kubectl_link)
            env_link = {
                "CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765",
                "CREDENTIAL_PROXY_EXEC_URL": "",
                "KUBE_PROXY_URL": "",
                "PATH": f"{bin_link}:{os.environ.get('PATH', '')}",
            }
            stall_report._SHIM_CACHE.clear()
            with patch.dict(os.environ, env_link, clear=False), \
                 patch.object(stall_report, "_ORIG_SUBPROCESS_RUN", stall_report.subprocess.run):
                self.assertIs(stall_report._shim_client(), credential_proxy_client)
                # 4. chmod 0o777 on a watched target behind the launcher (preserving mtime/size)
                #    must invalidate _SHIM_CACHE via st_mode in _stat_sig.
                target_a = os.path.join(dir_a, "credential_proxy_client.py")
                orig_stat = os.stat(target_a)
                os.chmod(target_a, 0o777)
                os.utime(target_a, ns=(orig_stat.st_atime_ns, orig_stat.st_mtime_ns))
                self.assertIsNone(stall_report._shim_client())
                os.chmod(target_a, 0o644)
                os.utime(target_a, ns=(orig_stat.st_atime_ns, orig_stat.st_mtime_ns))
                self.assertIs(stall_report._shim_client(), credential_proxy_client)

                os.unlink(kubectl_link)
                os.symlink(launcher_b, kubectl_link)
                self.assertIsNone(stall_report._shim_client())

    def test_parallel_collect_and_collect_cluster_emit_malformed_exec_url_notice_deterministically(self):
        import shutil
        import tempfile
        import threading
        import time
        import credential_proxy_client

        with tempfile.TemporaryDirectory() as tmp:
            shim_path = os.path.join(tmp, "kubectl")
            shutil.copyfile(credential_proxy_client.__file__, shim_path)
            os.chmod(shim_path, 0o755)
            env = {
                "CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765",
                "CREDENTIAL_PROXY_EXEC_URL": "https://bad-scheme.invalid:8766",
                "KUBE_PROXY_URL": "",
                "PATH": f"{tmp}:{os.environ.get('PATH', '')}",
            }
            saved_flag = credential_proxy_client._exec_url_notice_written
            try:
                # 1. Across 5 concurrent collect() runs with staggered completion, the notice
                #    always lands deterministically at stderr line 0.
                for run_idx in range(5):
                    credential_proxy_client._exec_url_notice_written = False
                    stall_report._SHIM_CACHE.clear()
                    barrier = threading.Barrier(6, timeout=5.0)

                    def fake_exec(endpoint, argv, *, fallback=None):
                        kind = argv[2]
                        if kind in ("k1", "k2", "k3", "k4", "k5", "k6"):
                            barrier.wait()
                            if kind == "k1":
                                time.sleep(0.02)
                            return 0, '{"items":[]}', f"{kind}-warn\n"
                        return 0, '{"items":[]}', ""

                    err_buf = io.StringIO()
                    with patch.dict(os.environ, env, clear=False), \
                         patch.object(credential_proxy_client, "execute_captured", side_effect=fake_exec), \
                         patch.object(stall_report, "_ORIG_SUBPROCESS_RUN", stall_report.subprocess.run), \
                         redirect_stderr(err_buf):
                        stall_report.collect(
                            NAMESPACE,
                            "k1,k2,k3,k4,k5,k6",
                            workers=stall_report.MAX_COLLECT_WORKERS,
                        )
                    lines = [l.strip() for l in err_buf.getvalue().splitlines() if l.strip()]
                    self.assertTrue(
                        lines and "CREDENTIAL_PROXY_EXEC_URL" in lines[0],
                        f"run {run_idx}: expected notice at line 0, got {lines!r}",
                    )
                    self.assertEqual(lines[1:], [f"warning: k{i}-warn" for i in range(1, 7)])

                # 2. On total failure in collect(), the notice is still emitted at line 0 (never dropped).
                credential_proxy_client._exec_url_notice_written = False
                stall_report._SHIM_CACHE.clear()
                err_buf = io.StringIO()
                with patch.dict(os.environ, env, clear=False), \
                     patch.object(
                         credential_proxy_client,
                         "execute_captured",
                         return_value=(1, "", "boom"),
                     ), \
                     patch.object(stall_report, "_ORIG_SUBPROCESS_RUN", stall_report.subprocess.run), \
                     redirect_stderr(err_buf):
                    with self.assertRaises(RuntimeError):
                        stall_report.collect(NAMESPACE, "deployments,pods")
                lines = [l.strip() for l in err_buf.getvalue().splitlines() if l.strip()]
                self.assertTrue(lines and "CREDENTIAL_PROXY_EXEC_URL" in lines[0], lines)

                # 3. collect_cluster() emits the notice deterministically at line 0.
                credential_proxy_client._exec_url_notice_written = False
                stall_report._SHIM_CACHE.clear()
                err_buf = io.StringIO()
                with patch.dict(os.environ, env, clear=False), \
                     patch.object(
                         credential_proxy_client,
                         "execute_captured",
                         return_value=(0, '{"items":[],"metadata":{}}', ""),
                     ), \
                     patch.object(stall_report, "_ORIG_SUBPROCESS_RUN", stall_report.subprocess.run), \
                     redirect_stderr(err_buf):
                    stall_report.collect_cluster(["deployments", "pods"])
                lines = [l.strip() for l in err_buf.getvalue().splitlines() if l.strip()]
                self.assertEqual(len(lines), 1)
                self.assertIn("CREDENTIAL_PROXY_EXEC_URL", lines[0])
            finally:
                credential_proxy_client._exec_url_notice_written = saved_flag

    def test_workers_one_disables_thread_pool_while_keeping_inprocess_shim(self):
        import shutil
        import tempfile
        import threading
        import credential_proxy_client

        with tempfile.TemporaryDirectory() as tmp:
            shim_path = os.path.join(tmp, "kubectl")
            shutil.copyfile(credential_proxy_client.__file__, shim_path)
            os.chmod(shim_path, 0o755)
            env = {
                "CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765",
                "CREDENTIAL_PROXY_EXEC_URL": "",
                "KUBE_PROXY_URL": "",
                "PATH": f"{tmp}:{os.environ.get('PATH', '')}",
            }
            lock = threading.Lock()
            in_flight = 0
            peak_in_flight = 0
            seen_argvs: list[list[str]] = []

            def fake_exec(endpoint, argv, *, fallback=None):
                nonlocal in_flight, peak_in_flight
                with lock:
                    in_flight += 1
                    peak_in_flight = max(peak_in_flight, in_flight)
                    seen_argvs.append(list(argv))
                try:
                    if "-o" in argv and argv[argv.index("-o") + 1] == "name":
                        return 0, "", ""
                    return 0, '{"items":[],"metadata":{}}', ""
                finally:
                    with lock:
                        in_flight -= 1

            def forbid_pool(*args, **kwargs):
                raise AssertionError("ThreadPoolExecutor must not be constructed when workers=1")

            dep_with_refs = obj(
                "Deployment",
                "web",
                spec={
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "envFrom": [
                                        {"configMapRef": {"name": "missing-cm"}},
                                        {"secretRef": {"name": "missing-sec"}},
                                    ]
                                }
                            ]
                        }
                    }
                },
                managed_fields=[managed(60)],
            )

            stall_report._SHIM_CACHE.clear()
            with patch.dict(os.environ, env, clear=False), \
                 patch.object(credential_proxy_client, "execute_captured", side_effect=fake_exec), \
                 patch.object(stall_report, "_ORIG_SUBPROCESS_RUN", stall_report.subprocess.run), \
                 patch.object(stall_report, "ThreadPoolExecutor", side_effect=forbid_pool):
                objects, events = stall_report.collect(NAMESPACE, "deployments,pods,services", workers=1)
                self.assertEqual((objects, events), ([], []))

                c_objects, c_events, c_fallback = stall_report.collect_cluster(
                    ["deployments", "pods", "services"], workers=1
                )
                self.assertEqual(c_fallback, [])
                self.assertEqual(c_events, {})
                self.assertEqual(set(c_objects), {"deployments", "pods", "services"})

                findings = stall_report.analyze(
                    [dep_with_refs], [], stall_report.NameResolver(), NOW, workers=1
                )
                self.assertEqual(len(findings), 2)

            self.assertEqual(peak_in_flight, 1)
            # 4 collect calls + 4 collect_cluster calls + 2 lazy NameResolver calls
            self.assertEqual(len(seen_argvs), 10)

    def test_workers_flag_clamping_validation_and_pool_sizing(self):
        import concurrent.futures
        import shutil
        import tempfile
        import credential_proxy_client

        # Default stays within broker session (2) and half of profile (4) caps;
        # _effective_workers clamps into [MIN_COLLECT_WORKERS, MAX_COLLECT_WORKERS].
        self.assertEqual(stall_report.DEFAULT_COLLECT_WORKERS, 2)
        self.assertEqual(stall_report.MAX_COLLECT_WORKERS, 6)
        self.assertEqual(stall_report._effective_workers(-5), stall_report.MIN_COLLECT_WORKERS)
        self.assertEqual(stall_report._effective_workers(0), stall_report.MIN_COLLECT_WORKERS)
        self.assertEqual(stall_report._effective_workers(1), 1)
        self.assertEqual(stall_report._effective_workers(3), 3)
        self.assertEqual(stall_report._effective_workers(99), stall_report.MAX_COLLECT_WORKERS)

        # CLI rejects values < 1
        for bad in ("0", "-1"):
            with self.subTest(workers=bad), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    stall_report.main(["--namespace", NAMESPACE, "--workers", bad, "--json"])
                self.assertEqual(ctx.exception.code, 2)

        # CLI defaults to DEFAULT_COLLECT_WORKERS (2) when omitted
        with patch.object(stall_report, "collect", return_value=([], [])) as mock_collect, \
             patch.object(stall_report, "analyze", return_value=[]) as mock_analyze, \
             redirect_stdout(io.StringIO()):
            rc = stall_report.main(["--namespace", NAMESPACE, "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(mock_collect.call_args.kwargs["workers"], stall_report.DEFAULT_COLLECT_WORKERS)
        self.assertEqual(mock_analyze.call_args.kwargs["workers"], stall_report.DEFAULT_COLLECT_WORKERS)

        # CLI clamps > MAX_COLLECT_WORKERS and threads workers to collect/analyze/report_cluster
        with patch.object(stall_report, "collect", return_value=([], [])) as mock_collect, \
             patch.object(stall_report, "analyze", return_value=[]) as mock_analyze, \
             redirect_stdout(io.StringIO()):
            rc = stall_report.main(["--namespace", NAMESPACE, "--workers", "99", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(mock_collect.call_args.kwargs["workers"], stall_report.MAX_COLLECT_WORKERS)
        self.assertEqual(mock_analyze.call_args.kwargs["workers"], stall_report.MAX_COLLECT_WORKERS)

        cluster_out = {"namespaces": {NAMESPACE: {"namespace": NAMESPACE, "stalled_resources": 0, "findings": []}}, "fallback": [], "events_fallback": False}
        with patch.object(stall_report, "report_cluster", return_value=cluster_out) as mock_rc, \
             redirect_stdout(io.StringIO()):
            rc = stall_report.main(["--namespaces", NAMESPACE, "--workers", "1", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(mock_rc.call_args.kwargs["workers"], 1)

        # ThreadPoolExecutor receives max_workers=2 by default and max_workers=3 when workers=3
        observed_max_workers: list[int] = []
        real_pool = concurrent.futures.ThreadPoolExecutor

        def tracking_pool(*, max_workers):
            observed_max_workers.append(max_workers)
            return real_pool(max_workers=max_workers)

        with tempfile.TemporaryDirectory() as tmp:
            shim_path = os.path.join(tmp, "kubectl")
            shutil.copyfile(credential_proxy_client.__file__, shim_path)
            os.chmod(shim_path, 0o755)
            env = {
                "CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765",
                "CREDENTIAL_PROXY_EXEC_URL": "",
                "KUBE_PROXY_URL": "",
                "PATH": f"{tmp}:{os.environ.get('PATH', '')}",
            }
            dep_with_refs = obj(
                "Deployment",
                "web",
                spec={
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "envFrom": [
                                        {"configMapRef": {"name": "cm-a"}},
                                        {"secretRef": {"name": "sec-a"}},
                                    ]
                                }
                            ],
                            "volumes": [
                                {"name": "data", "persistentVolumeClaim": {"claimName": "pvc-a"}},
                            ],
                        }
                    }
                },
                managed_fields=[managed(60)],
            )
            stall_report._SHIM_CACHE.clear()
            with patch.dict(os.environ, env, clear=False), \
                 patch.object(
                     credential_proxy_client,
                     "execute_captured",
                     return_value=(0, '{"items":[],"metadata":{}}', ""),
                 ), \
                 patch.object(stall_report, "_ORIG_SUBPROCESS_RUN", stall_report.subprocess.run), \
                 patch.object(stall_report, "ThreadPoolExecutor", side_effect=tracking_pool):
                stall_report.collect(NAMESPACE, "deployments,pods,services,jobs")
                stall_report.collect_cluster(["deployments", "pods", "services", "jobs"])
                stall_report.prefetch_references(stall_report.NameResolver(), [dep_with_refs], NOW)
                stall_report.collect(NAMESPACE, "deployments,pods,services,jobs", workers=3)
                stall_report.collect_cluster(["deployments", "pods", "services", "jobs"], workers=3)
                stall_report.prefetch_references(stall_report.NameResolver(), [dep_with_refs], NOW, workers=3)
        self.assertEqual(observed_max_workers, [2, 2, 2, 3, 3, 3])


if __name__ == "__main__":
    unittest.main()
