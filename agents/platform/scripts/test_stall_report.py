#!/usr/bin/env python3
"""Unit tests for stall_report.py: fixture objects, kubectl stubbed."""

import io
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


def analyze(objects, events=(), existing=None, override=None):
    resolver = FakeResolver(existing or {})
    return stall_report.analyze(list(objects), list(events), resolver, NOW, override)


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
        listing = "deployments.apps\nevents\nevents.events.k8s.io\nsecrets\nendpoints\npods.metrics.k8s.io\ngateways.gateway.networking.k8s.io\n"
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

    def test_collect_passes_kinds_and_managed_fields(self):
        calls = []

        def fake_json(args):
            calls.append(args)
            return {"items": []}

        with patch.object(stall_report, "kubectl_json", side_effect=fake_json):
            stall_report.collect(NAMESPACE, "deployments,gateways")
        self.assertEqual(calls[0], ["get", "deployments,gateways", "-n", NAMESPACE, "--show-managed-fields"])
        self.assertEqual(calls[1], ["get", "events", "-n", NAMESPACE])


if __name__ == "__main__":
    unittest.main()
