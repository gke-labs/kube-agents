#!/usr/bin/env python3
"""Unit tests for the #3285 single-pass cluster domain auditors:
- audit_certificates.py
- audit_storage.py
- audit_networking.py
- audit_network_policies.py
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import audit_certificates  # noqa: E402
import audit_network_policies  # noqa: E402
import audit_networking  # noqa: E402
import audit_storage  # noqa: E402


class TestAuditCertificates(unittest.TestCase):
    def test_absent_crds_with_tls_secrets_emits_clean_stop(self):
        secret_args = []

        def fake_run_cmd(args, env):
            if "secrets" in args:
                secret_args.extend(args)
                return 0, (
                    "kubeagents-system   webhook-tls   2026-10-14T10:00:00Z\n"
                    "default             app-tls       2026-10-15T10:00:00Z"
                )
            return 0, "agentprofiles.kubeagents.x-k8s.io\n"

        def fake_run_kubectl_json(args, env):
            return 0, {"items": []}

        buf = io.StringIO()
        with (
            mock.patch.object(audit_certificates, "run_cmd", side_effect=fake_run_cmd),
            mock.patch.object(audit_certificates, "run_kubectl_json", side_effect=fake_run_kubectl_json),
            contextlib.redirect_stdout(buf),
        ):
            rc = audit_certificates.audit_certificates()

        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("- **cert_manager_installed:** `false`", out)
        self.assertIn("2 secret(s) across 2 namespace(s) [default (1), kubeagents-system (1)]", out)
        self.assertIn("not checked for expiry", out)
        self.assertIn("CLEAN_AUDIT_STOP: YES", out)
        # Secrets are listed metadata-only: no -o json, so no tls.key payload is fetched.
        self.assertNotIn("json", secret_args)
        self.assertIn(audit_certificates.TLS_SECRET_COLUMNS, secret_args)

    def test_partial_crds_zero_objects_and_rbac_forbidden_secrets_emits_clean_stop(self):
        """Regression test for R6 P09 on hub clusters where only certificates & clusterissuers CRDs exist."""

        def fake_run_cmd(args, env):
            if "secrets" in args:
                return 1, "Error from server (Forbidden): secrets is forbidden"
            return 0, "certificates.cert-manager.io\nclusterissuers.cert-manager.io\n"

        def fake_run_kubectl_json(args, env):
            return 0, {"items": []}

        buf = io.StringIO()
        with (
            mock.patch.object(audit_certificates, "run_cmd", side_effect=fake_run_cmd),
            mock.patch.object(audit_certificates, "run_kubectl_json", side_effect=fake_run_kubectl_json),
            contextlib.redirect_stdout(buf),
        ):
            rc = audit_certificates.audit_certificates()

        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("- **cert_manager_installed:** `true`", out)
        self.assertIn("Partial CRD installation", out)
        self.assertIn("not counted (RBAC forbids cluster-wide Secret listing)", out)
        self.assertNotIn("0 TLS errors", out)
        self.assertNotIn("count only", out)
        self.assertIn("native TLS Secrets were not counted", out)
        self.assertIn("CLEAN_AUDIT_STOP: YES — No failed, expired, or renewal-overdue certificates", out)

    def test_healthy_active_certs_and_issuers_emits_clean_stop(self):
        def fake_run_cmd(args, env):
            return 0, "\n".join(audit_certificates.CERT_MANAGER_CRDS)

        def fake_run_kubectl_json(args, env):
            if "certificates.cert-manager.io" in args:
                return 0, {
                    "items": [
                        {
                            "metadata": {"namespace": "kubeagents-system", "name": "serving-cert"},
                            "spec": {
                                "secretName": "webhook-certs",
                                "issuerRef": {"kind": "Issuer", "name": "selfsigned"},
                            },
                            "status": {
                                "notAfter": "2099-01-01T00:00:00Z",
                                "conditions": [{"type": "Ready", "status": "True", "reason": "Ready"}],
                            },
                        }
                    ]
                }
            if "issuers.cert-manager.io" in args:
                return 0, {
                    "items": [
                        {
                            "metadata": {"namespace": "kubeagents-system", "name": "selfsigned"},
                            "spec": {"selfSigned": {}},
                            "status": {"conditions": [{"type": "Ready", "status": "True", "reason": "IsReady"}]},
                        }
                    ]
                }
            return 0, {"items": []}

        buf = io.StringIO()
        with (
            mock.patch.object(audit_certificates, "run_cmd", side_effect=fake_run_cmd),
            mock.patch.object(audit_certificates, "run_kubectl_json", side_effect=fake_run_kubectl_json),
            contextlib.redirect_stdout(buf),
        ):
            rc = audit_certificates.audit_certificates()

        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("1 total (1 Ready, 0 Unready/Degraded)", out)
        self.assertIn("CLEAN_AUDIT_STOP: YES", out)

    def test_degraded_certificate_emits_clean_stop_no(self):
        def fake_run_cmd(args, env):
            return 0, "\n".join(audit_certificates.CERT_MANAGER_CRDS)

        def fake_run_kubectl_json(args, env):
            if "certificates.cert-manager.io" in args:
                return 0, {
                    "items": [
                        {
                            "metadata": {"namespace": "prod", "name": "broken-cert"},
                            "spec": {
                                "secretName": "broken-tls",
                                "issuerRef": {"kind": "ClusterIssuer", "name": "letsencrypt"},
                            },
                            "status": {
                                "conditions": [
                                    {
                                        "type": "Ready",
                                        "status": "False",
                                        "reason": "Issuing",
                                        "message": "Waiting for ACME challenge",
                                    }
                                ]
                            },
                        }
                    ]
                }
            return 0, {"items": []}

        buf = io.StringIO()
        with (
            mock.patch.object(audit_certificates, "run_cmd", side_effect=fake_run_cmd),
            mock.patch.object(audit_certificates, "run_kubectl_json", side_effect=fake_run_kubectl_json),
            contextlib.redirect_stdout(buf),
        ):
            rc = audit_certificates.audit_certificates()

        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("Degraded Certificates Detected", out)
        self.assertIn("CLEAN_AUDIT_STOP: NO", out)
        self.assertIn("Waiting for ACME challenge", out)


    def _run_certs(self, crd_listing, kubectl_json, secrets=(0, "")):
        def fake_run_cmd(args, env):
            return secrets if "secrets" in args else crd_listing

        buf = io.StringIO()
        with (
            mock.patch.object(audit_certificates, "run_cmd", side_effect=fake_run_cmd),
            mock.patch.object(audit_certificates, "run_kubectl_json", side_effect=kubectl_json),
            contextlib.redirect_stdout(buf),
        ):
            rc = audit_certificates.audit_certificates()
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_failed_crd_listing_is_unread_not_absent(self):
        out = self._run_certs((1, "Unable to connect to the server: i/o timeout"), lambda a, e: (0, {"items": []}))
        self.assertIn("CLEAN_AUDIT_STOP: NO", out)
        self.assertIn("i/o timeout", out)
        self.assertNotIn("NOT INSTALLED", out)

    def test_forbidden_certificate_listing_is_unread(self):
        def kubectl_json(args, env):
            if "certificates.cert-manager.io" in args:
                return 1, {"stderr": "Error from server (Forbidden): certificates.cert-manager.io is forbidden"}
            return 0, {"items": []}

        out = self._run_certs((0, "\n".join(audit_certificates.CERT_MANAGER_CRDS)), kubectl_json)
        self.assertIn("CLEAN_AUDIT_STOP: NO", out)
        self.assertIn("Could not read `certificates`", out)
        self.assertIn("- **Certificates:** " + audit_certificates.UNREAD_COUNT_TEXT, out)
        self.assertNotIn("**Certificates:** 0 total", out)
        self.assertNotIn("No Certificate resources declared", out)

    def _expired_certificate(self, expired_for):
        not_after = (datetime.now(timezone.utc) - expired_for).isoformat()

        def kubectl_json(args, env):
            if "certificates.cert-manager.io" in args:
                return 0, {
                    "items": [
                        {
                            "metadata": {"namespace": "prod", "name": "lapsed"},
                            "spec": {"secretName": "s", "issuerRef": {"kind": "ClusterIssuer", "name": "acme"}},
                            "status": {"notAfter": not_after, "conditions": [{"type": "Ready", "status": "True"}]},
                        }
                    ]
                }
            return 0, {"items": []}

        return self._run_certs((0, "\n".join(audit_certificates.CERT_MANAGER_CRDS)), kubectl_json)

    def test_certificate_expired_minutes_ago_prints_zero_days_ago(self):
        out = self._expired_certificate(timedelta(minutes=10))
        self.assertIn("EXPIRED (0d ago)", out)
        self.assertIn("CLEAN_AUDIT_STOP: NO", out)

    def test_certificate_expired_twenty_five_hours_ago_prints_one_day_ago(self):
        out = self._expired_certificate(timedelta(hours=25))
        self.assertIn("EXPIRED (1d ago)", out)

    def test_failing_managed_certificate_without_cert_manager_is_degraded(self):
        def kubectl_json(args, env):
            return 0, {
                "items": [
                    {
                        "metadata": {"namespace": "prod", "name": "web-cert"},
                        "status": {"certificateStatus": "FailedNotVisible"},
                    }
                ]
            }

        out = self._run_certs((0, audit_certificates.MANAGED_CERT_CRD), kubectl_json)
        self.assertIn("- **cert_manager_installed:** `false`", out)
        self.assertIn("ManagedCertificate `prod/web-cert`: FailedNotVisible", out)
        self.assertIn("CLEAN_AUDIT_STOP: NO", out)

    def test_ready_certificate_past_renewal_or_failing_issuance_is_degraded(self):
        def kubectl_json(args, env):
            if "certificates.cert-manager.io" in args:
                base = {
                    "spec": {"secretName": "s", "issuerRef": {"kind": "ClusterIssuer", "name": "acme"}},
                }
                ready = {"type": "Ready", "status": "True", "reason": "Ready"}
                return 0, {
                    "items": [
                        dict(
                            base,
                            metadata={"namespace": "prod", "name": "overdue"},
                            status={
                                "notAfter": "2099-01-01T00:00:00Z",
                                "renewalTime": "2000-01-01T00:00:00Z",
                                "conditions": [ready],
                            },
                        ),
                        dict(
                            base,
                            metadata={"namespace": "prod", "name": "failing"},
                            status={
                                "notAfter": "2099-01-01T00:00:00Z",
                                "conditions": [
                                    ready,
                                    {"type": "Issuing", "status": "False", "reason": "Failed", "message": "ACME order failed"},
                                ],
                            },
                        ),
                    ]
                }
            return 0, {"items": []}

        out = self._run_certs((0, "\n".join(audit_certificates.CERT_MANAGER_CRDS)), kubectl_json)
        self.assertIn("2 total (0 Ready, 2 Unready/Degraded)", out)
        self.assertIn("RenewalOverdue", out)
        self.assertIn("ACME order failed", out)
        self.assertIn("CLEAN_AUDIT_STOP: NO", out)

    def test_failed_non_forbidden_secret_listing_is_unread(self):
        out = self._run_certs(
            (0, "\n".join(audit_certificates.CERT_MANAGER_CRDS)),
            lambda a, e: (0, {"items": []}),
            secrets=(1, "Unable to connect to the server: i/o timeout"),
        )
        self.assertIn("Could not read `tls_secrets`: Unable to connect", out)
        self.assertIn("CLEAN_AUDIT_STOP: NO", out)
        self.assertNotIn("CLEAN_AUDIT_STOP: YES", out)

    def _renewing_certificate(self, issuing_since):
        def kubectl_json(args, env):
            if "certificates.cert-manager.io" in args:
                return 0, {
                    "items": [
                        {
                            "metadata": {"namespace": "prod", "name": "renewing"},
                            "spec": {"secretName": "s", "issuerRef": {"kind": "ClusterIssuer", "name": "acme"}},
                            "status": {
                                "notAfter": "2099-01-01T00:00:00Z",
                                "renewalTime": "2000-01-01T00:00:00Z",
                                "conditions": [
                                    {"type": "Ready", "status": "True", "reason": "Ready"},
                                    {
                                        "type": "Issuing",
                                        "status": "True",
                                        "reason": "Renewing",
                                        "lastTransitionTime": issuing_since.isoformat(),
                                    },
                                ],
                            },
                        }
                    ]
                }
            return 0, {"items": []}

        return self._run_certs((0, "\n".join(audit_certificates.CERT_MANAGER_CRDS)), kubectl_json)

    def test_certificate_past_renewal_time_while_issuing_is_not_overdue(self):
        out = self._renewing_certificate(datetime.now(timezone.utc))
        self.assertNotIn("RenewalOverdue", out)
        self.assertIn("CLEAN_AUDIT_STOP: YES", out)

    def test_certificate_stuck_issuing_past_grace_is_overdue(self):
        stale = datetime.now(timezone.utc) - audit_certificates.RENEWAL_IN_PROGRESS_GRACE - timedelta(minutes=1)
        out = self._renewing_certificate(stale)
        self.assertIn("RenewalOverdue", out)
        self.assertIn("CLEAN_AUDIT_STOP: NO", out)


class TestAuditStorage(unittest.TestCase):
    def test_healthy_storage_emits_clean_stop(self):
        def fake_run_kubectl_json(args, env):
            if "pvc" in args:
                return 0, {
                    "items": [
                        {
                            "metadata": {"namespace": "default", "name": "data-pvc"},
                            "spec": {
                                "volumeName": "pv-1",
                                "storageClassName": "standard-rwo",
                                "resources": {"requests": {"storage": "10Gi"}},
                            },
                            "status": {"phase": "Bound"},
                        }
                    ]
                }
            if "pv" in args:
                return 0, {
                    "items": [
                        {
                            "metadata": {"name": "pv-1"},
                            "spec": {
                                "capacity": {"storage": "10Gi"},
                                "persistentVolumeReclaimPolicy": "Delete",
                                "claimRef": {"namespace": "default", "name": "data-pvc"},
                            },
                            "status": {"phase": "Bound"},
                        }
                    ]
                }
            if "sc" in args:
                return 0, {
                    "items": [
                        {
                            "metadata": {
                                "name": "standard-rwo",
                                "annotations": {"storageclass.kubernetes.io/is-default-class": "true"},
                            },
                            "provisioner": "pd.csi.storage.gke.io",
                            "reclaimPolicy": "Delete",
                            "volumeBindingMode": "WaitForFirstConsumer",
                        }
                    ]
                }
            return 0, {"items": []}

        buf = io.StringIO()
        with (
            mock.patch.object(audit_storage, "run_kubectl_json", side_effect=fake_run_kubectl_json),
            contextlib.redirect_stdout(buf),
        ):
            rc = audit_storage.audit_storage()

        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("Storage Audit Verdict: Clean & Fully Healthy", out)
        self.assertIn("CLEAN_AUDIT_STOP: YES", out)

    def test_degraded_storage_emits_stop_no(self):
        def fake_run_kubectl_json(args, env):
            if "pvc" in args:
                return 0, {
                    "items": [
                        {
                            "metadata": {"namespace": "default", "name": "stuck-pvc"},
                            "spec": {"storageClassName": "missing-sc"},
                            "status": {"phase": "Pending"},
                        }
                    ]
                }
            return 0, {"items": []}

        buf = io.StringIO()
        with (
            mock.patch.object(audit_storage, "run_kubectl_json", side_effect=fake_run_kubectl_json),
            contextlib.redirect_stdout(buf),
        ):
            rc = audit_storage.audit_storage()

        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("Storage Audit Verdict: Degradation Detected", out)
        self.assertIn("CLEAN_AUDIT_STOP: NO", out)

    def _storage(self, pvc_annotations):
        def fake_run_kubectl_json(args, env):
            if "pvc" in args:
                return 0, {
                    "items": [
                        {
                            "metadata": {"namespace": "prod", "name": "data", "annotations": pvc_annotations},
                            "spec": {"resources": {"requests": {"storage": "10Gi"}}},
                            "status": {"phase": "Pending"},
                        }
                    ]
                }
            if "sc" in args:
                return 0, {
                    "items": [
                        {
                            "metadata": {
                                "name": "standard-rwo",
                                "annotations": {"storageclass.kubernetes.io/is-default-class": "true"},
                            },
                            "provisioner": "pd.csi.storage.gke.io",
                            "volumeBindingMode": "WaitForFirstConsumer",
                        }
                    ]
                }
            return 0, {"items": []}

        buf = io.StringIO()
        with (
            mock.patch.object(audit_storage, "run_kubectl_json", side_effect=fake_run_kubectl_json),
            contextlib.redirect_stdout(buf),
        ):
            audit_storage.audit_storage()
        return buf.getvalue()

    def test_unclaimed_pvc_on_wait_for_first_consumer_class_is_not_degraded(self):
        out = self._storage({})
        self.assertIn("1 waiting for first consumer, 0 Pending/Unbound", out)
        self.assertIn("CLEAN_AUDIT_STOP: YES", out)

    def test_pending_pvc_with_a_selected_node_is_degraded(self):
        out = self._storage({audit_storage.SELECTED_NODE_ANNOTATION: "node-1"})
        self.assertIn("1 pending PVCs", out)
        self.assertIn("CLEAN_AUDIT_STOP: NO", out)

    def test_failed_listing_is_unread_not_empty(self):
        def fake_run_kubectl_json(args, env):
            if "pvc" in args:
                return 1, {"error": "Command timed out after 15 seconds"}
            return 0, {"items": []}

        buf = io.StringIO()
        with (
            mock.patch.object(audit_storage, "run_kubectl_json", side_effect=fake_run_kubectl_json),
            contextlib.redirect_stdout(buf),
        ):
            rc = audit_storage.audit_storage()

        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("Storage Audit Verdict: Incomplete** (could not read: pvcs)", out)
        self.assertIn("timed out", out)
        self.assertIn("- **PersistentVolumeClaims (PVCs):** " + audit_storage.UNREAD_COUNT_TEXT, out)
        self.assertNotIn("No PersistentVolumeClaims found", out)
        self.assertIn("No PersistentVolumes found", out)
        self.assertIn("CLEAN_AUDIT_STOP: NO", out)
        self.assertNotIn("CLEAN_AUDIT_STOP: YES", out)


class TestAuditNetworking(unittest.TestCase):
    def test_healthy_networking_emits_clean_stop(self):
        def fake_run_kubectl_json(args, env):
            if "svc" in args:
                return 0, {
                    "items": [
                        {
                            "metadata": {"namespace": "prod", "name": "frontend-lb"},
                            "spec": {
                                "type": "LoadBalancer",
                                "clusterIP": "10.0.0.10",
                                "ports": [{"port": 443, "targetPort": 8443, "protocol": "TCP"}],
                            },
                            "status": {"loadBalancer": {"ingress": [{"ip": "34.100.1.2"}]}},
                        }
                    ]
                }
            if "gateways.gateway.networking.k8s.io" in args:
                return 0, {
                    "items": [
                        {
                            "metadata": {"namespace": "prod", "name": "ext-gw"},
                            "spec": {"gatewayClassName": "gke-l7-global-external-managed"},
                            "status": {
                                "addresses": [{"value": "34.100.1.3"}],
                                "conditions": [{"type": "Programmed", "status": "True", "reason": "Programmed"}],
                            },
                        }
                    ]
                }
            return 0, {"items": []}

        buf = io.StringIO()
        with (
            mock.patch.object(audit_networking, "run_kubectl_json", side_effect=fake_run_kubectl_json),
            contextlib.redirect_stdout(buf),
        ):
            rc = audit_networking.audit_networking()

        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("Networking Audit Verdict: Clean & Fully Programmed", out)
        self.assertIn("CLEAN_AUDIT_STOP: YES", out)

    def test_degraded_networking_emits_stop_no(self):
        def fake_run_kubectl_json(args, env):
            if "svc" in args:
                return 0, {
                    "items": [
                        {
                            "metadata": {"namespace": "prod", "name": "pending-lb"},
                            "spec": {"type": "LoadBalancer", "clusterIP": "10.0.0.11", "ports": []},
                            "status": {"loadBalancer": {}},
                        }
                    ]
                }
            return 0, {"items": []}

        buf = io.StringIO()
        with (
            mock.patch.object(audit_networking, "run_kubectl_json", side_effect=fake_run_kubectl_json),
            contextlib.redirect_stdout(buf),
        ):
            rc = audit_networking.audit_networking()

        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("Networking Audit Verdict: Routing / VIP Degradation Detected", out)
        self.assertIn("CLEAN_AUDIT_STOP: NO", out)

    def _run_networking(self, fake_run_kubectl_json):
        buf = io.StringIO()
        with (
            mock.patch.object(audit_networking, "run_kubectl_json", side_effect=fake_run_kubectl_json),
            contextlib.redirect_stdout(buf),
        ):
            rc = audit_networking.audit_networking()
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_absent_optional_crd_is_empty_but_failed_core_kind_is_unread(self):
        def absent_crds(args, env):
            if any(".gateway.networking.k8s.io" in a or ".cloud.google.com" in a or ".networking.gke.io" in a for a in args):
                return 1, {"error": 'error: the server doesn\'t have a resource type "gateways"'}
            return 0, {"items": []}

        self.assertIn("CLEAN_AUDIT_STOP: YES", self._run_networking(absent_crds))

        def forbidden_ingress(args, env):
            if "ingress" in args:
                return 1, {"error": "Error from server (Forbidden): ingresses.networking.k8s.io is forbidden"}
            return 0, {"items": []}

        out = self._run_networking(forbidden_ingress)
        self.assertIn("could not read: ingresses", out)
        self.assertIn("CLEAN_AUDIT_STOP: NO", out)

        # An optional CRD that is Forbidden (not absent) is unread, as under the minimal ClusterRole.
        def forbidden_gateways(args, env):
            if "gateways.gateway.networking.k8s.io" in args:
                return 1, {"error": "Error from server (Forbidden): gateways.gateway.networking.k8s.io is forbidden"}
            return 0, {"items": []}

        out = self._run_networking(forbidden_gateways)
        self.assertIn("could not read: gateways", out)
        self.assertIn("CLEAN_AUDIT_STOP: NO", out)

        # The missing-resource-type text on a core kind is a failed read, not an absent CRD.
        def missing_type_ingress(args, env):
            if "ingress" in args:
                return 1, {"error": 'error: the server doesn\'t have a resource type "ingress"'}
            return 0, {"items": []}

        out = self._run_networking(missing_type_ingress)
        self.assertIn("could not read: ingresses", out)
        self.assertIn("CLEAN_AUDIT_STOP: NO", out)

    def test_gateway_programmed_false_and_failing_route_are_degraded(self):
        def fake_run_kubectl_json(args, env):
            if "gateways.gateway.networking.k8s.io" in args:
                return 0, {
                    "items": [
                        {
                            "metadata": {"namespace": "prod", "name": "ext-gw"},
                            "spec": {"gatewayClassName": "gke-l7-global-external-managed"},
                            "status": {
                                "addresses": [{"value": "34.100.1.3"}],
                                "conditions": [
                                    {"type": "Accepted", "status": "True", "reason": "Accepted"},
                                    {"type": "Programmed", "status": "False", "reason": "Invalid"},
                                ],
                            },
                        }
                    ]
                }
            if "httproutes.gateway.networking.k8s.io" in args:
                return 0, {
                    "items": [
                        {
                            "metadata": {"namespace": "prod", "name": "web"},
                            "spec": {"parentRefs": [{"name": "ext-gw"}]},
                            "status": {
                                "parents": [
                                    {
                                        "parentRef": {"name": "ext-gw"},
                                        "conditions": [{"type": "ResolvedRefs", "status": "False", "reason": "BackendNotFound"}],
                                    }
                                ]
                            },
                        }
                    ]
                }
            return 0, {"items": []}

        out = self._run_networking(fake_run_kubectl_json)
        self.assertIn("1 unaccepted Gateways, 1 failing HTTPRoutes", out)
        self.assertIn("NOT PROGRAMMED (Invalid)", out)
        web_line = next(line for line in out.splitlines() if "web" in line)
        self.assertIn("ResolvedRefs=False", web_line)
        self.assertNotIn("NotAttached", web_line)
        self.assertIn("CLEAN_AUDIT_STOP: NO", out)

    def test_gateway_with_no_status_conditions_reports_no_conditions_reason(self):
        def fake_run_kubectl_json(args, env):
            if "gateways.gateway.networking.k8s.io" in args:
                return 0, {
                    "items": [
                        {
                            "metadata": {"namespace": "prod", "name": "unreconciled-gw"},
                            "spec": {"gatewayClassName": "missing-class"},
                            "status": {},
                        }
                    ]
                }
            return 0, {"items": []}

        out = self._run_networking(fake_run_kubectl_json)
        self.assertIn("1 unaccepted Gateways", out)
        self.assertIn("NOT PROGRAMMED (no status conditions reported)", out)
        self.assertNotIn("NOT PROGRAMMED (Accepted)", out)
        self.assertIn("CLEAN_AUDIT_STOP: NO", out)

    def test_truncated_proxy_reply_is_reported_as_truncated_not_json_error(self):
        truncated = mock.Mock(
            returncode=0,
            stdout='{"items": [{"metadata": {"name": "cut',
            stderr="credential proxy output truncated at 4194304 bytes\n",
        )
        for mod in (audit_networking, audit_storage, audit_network_policies, audit_certificates):
            with self.subTest(module=mod.__name__):
                with mock.patch.object(mod.subprocess, "run", return_value=truncated):
                    code, data = mod.run_kubectl_json(["get", "svc"], {})
                self.assertNotEqual(code, 0)
                self.assertIn("credential proxy output truncated", data["error"])
        with mock.patch.object(audit_certificates.subprocess, "run", return_value=truncated):
            cmd_code, cmd_out = audit_certificates.run_cmd(["kubectl", "get", "crd"], {})
        self.assertNotEqual(cmd_code, 0)
        self.assertIn("credential proxy output truncated", cmd_out)

    def test_route_with_parent_refs_but_no_status_parents_is_not_attached(self):
        def fake_run_kubectl_json(args, env):
            if "httproutes.gateway.networking.k8s.io" in args:
                return 0, {
                    "items": [
                        {
                            "metadata": {"namespace": "prod", "name": "orphan"},
                            "spec": {"parentRefs": [{"name": "missing-gw"}]},
                            "status": {"parents": []},
                        }
                    ]
                }
            return 0, {"items": []}

        out = self._run_networking(fake_run_kubectl_json)
        self.assertIn("1 failing HTTPRoutes", out)
        orphan_line = next(line for line in out.splitlines() if "orphan" in line)
        self.assertIn("NotAttached", orphan_line)
        self.assertIn("CLEAN_AUDIT_STOP: NO", out)

    def test_route_attached_to_only_one_of_two_parents_is_degraded(self):
        def fake_run_kubectl_json(args, env):
            if "httproutes.gateway.networking.k8s.io" in args:
                return 0, {
                    "items": [
                        {
                            "metadata": {"namespace": "prod", "name": "split"},
                            "spec": {"parentRefs": [{"name": "gw-a"}, {"name": "gw-b"}]},
                            "status": {
                                "parents": [
                                    {"parentRef": {"name": "gw-a"}, "conditions": [{"type": "Accepted", "status": "True"}]}
                                ]
                            },
                        }
                    ]
                }
            return 0, {"items": []}

        out = self._run_networking(fake_run_kubectl_json)
        self.assertIn("1 failing HTTPRoutes", out)
        self.assertIn("CLEAN_AUDIT_STOP: NO", out)
        split_row = next(line for line in out.splitlines() if line.startswith("prod") and "split" in line)
        self.assertIn("Accepted=True, NotAttached", split_row)

    def _ingress_with_backends(self, backends_annotation):
        def fake_run_kubectl_json(args, env):
            if "ingress" in args:
                return 0, {
                    "items": [
                        {
                            "metadata": {
                                "namespace": "prod",
                                "name": "web",
                                "annotations": {audit_networking.GKE_BACKENDS_ANNOTATION: backends_annotation},
                            },
                            "spec": {"rules": [{"host": "web.example.com"}]},
                            "status": {"loadBalancer": {"ingress": [{"ip": "34.100.1.9"}]}},
                        }
                    ]
                }
            return 0, {"items": []}

        return self._run_networking(fake_run_kubectl_json)

    def test_ingress_with_unhealthy_gke_backend_is_degraded(self):
        out = self._ingress_with_backends('{"k8s1-abc-prod-web-80": "UNHEALTHY", "k8s1-abc-default-be-80": "HEALTHY"}')
        self.assertIn("1 Ingresses with unhealthy backends", out)
        self.assertIn("k8s1-abc-prod-web-80=UNHEALTHY", out)
        self.assertIn("CLEAN_AUDIT_STOP: NO", out)

    def test_ingress_with_unparseable_backends_annotation_is_degraded(self):
        out = self._ingress_with_backends("not-json")
        self.assertIn("unparseable", out)
        self.assertIn("CLEAN_AUDIT_STOP: NO", out)

    def test_ingress_with_all_backends_healthy_is_clean(self):
        out = self._ingress_with_backends('{"k8s1-abc-prod-web-80": "HEALTHY"}')
        self.assertIn("CLEAN_AUDIT_STOP: YES", out)

    def test_failed_service_listing_prints_unread_not_zero_counts(self):
        def fake_run_kubectl_json(args, env):
            if "svc" in args:
                return 1, {"error": "Error from server (Forbidden): services is forbidden"}
            return 0, {"items": []}

        out = self._run_networking(fake_run_kubectl_json)
        self.assertIn("- **LoadBalancer Services:** " + audit_networking.UNREAD_COUNT_TEXT, out)
        self.assertNotIn("0 Active VIP", out)
        self.assertIn("CLEAN_AUDIT_STOP: NO", out)

    def test_route_with_duplicate_status_for_one_parent_and_none_for_the_other_is_degraded(self):
        def fake_run_kubectl_json(args, env):
            if "httproutes.gateway.networking.k8s.io" in args:
                accepted = [{"type": "Accepted", "status": "True"}]
                return 0, {
                    "items": [
                        {
                            "metadata": {"namespace": "prod", "name": "dup"},
                            "spec": {"parentRefs": [{"name": "gw-a"}, {"name": "gw-b"}]},
                            "status": {
                                "parents": [
                                    {"parentRef": {"name": "gw-a"}, "controllerName": "c1", "conditions": accepted},
                                    {"parentRef": {"name": "gw-a"}, "controllerName": "c2", "conditions": accepted},
                                ]
                            },
                        }
                    ]
                }
            return 0, {"items": []}

        out = self._run_networking(fake_run_kubectl_json)
        self.assertIn("1 failing HTTPRoutes", out)
        self.assertIn("CLEAN_AUDIT_STOP: NO", out)

    def test_programmed_gateway_with_failing_listener_is_degraded(self):
        def fake_run_kubectl_json(args, env):
            if "gateways.gateway.networking.k8s.io" in args:
                return 0, {
                    "items": [
                        {
                            "metadata": {"namespace": "prod", "name": "ext-gw"},
                            "spec": {"gatewayClassName": "istio"},
                            "status": {
                                "addresses": [{"value": "34.100.1.3"}],
                                "conditions": [
                                    {"type": "Accepted", "status": "True", "reason": "Accepted"},
                                    {"type": "Programmed", "status": "True", "reason": "Programmed"},
                                ],
                                "listeners": [
                                    {
                                        "name": "https",
                                        "conditions": [
                                            {"type": "ResolvedRefs", "status": "False", "reason": "InvalidCertificateRef"}
                                        ],
                                    }
                                ],
                            },
                        }
                    ]
                }
            return 0, {"items": []}

        out = self._run_networking(fake_run_kubectl_json)
        self.assertIn("1 unaccepted Gateways", out)
        self.assertIn("listener https: ResolvedRefs=False InvalidCertificateRef", out)
        self.assertIn("CLEAN_AUDIT_STOP: NO", out)


class TestAuditNetworkPolicies(unittest.TestCase):
    def test_network_policies_summary_and_clean_stop(self):
        def fake_run_kubectl_json(args, env):
            if "ns" in args:
                return 0, {
                    "items": [
                        {"metadata": {"name": "default"}},
                        {"metadata": {"name": "kube-system"}},
                        {"metadata": {"name": "prod"}},
                    ]
                }
            if "networkpolicies" in args:
                return 0, {
                    "items": [
                        {
                            "metadata": {"namespace": "prod", "name": "deny-all"},
                            "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
                        },
                        {
                            "metadata": {"namespace": "prod", "name": "db-only"},
                            "spec": {
                                "podSelector": {"matchExpressions": [{"key": "tier", "operator": "In", "values": ["db"]}]},
                                "policyTypes": ["Ingress"],
                            },
                        },
                    ]
                }
            return 0, {"items": []}

        buf = io.StringIO()
        with (
            mock.patch.object(audit_network_policies, "run_kubectl_json", side_effect=fake_run_kubectl_json),
            contextlib.redirect_stdout(buf),
        ):
            rc = audit_network_policies.audit_network_policies()

        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("- **Total Namespaces:** 3", out)
        self.assertIn("- **Namespaces Protected by NetworkPolicies:** 1", out)
        self.assertIn("- **Namespaces Without NetworkPolicies:** 1", out)
        self.assertIn("Isolation Gaps Found", out)
        self.assertIn("CLEAN_AUDIT_STOP: NO", out)
        self.assertNotIn("CLEAN_AUDIT_STOP: YES", out)
        self.assertIn("### Unprotected Namespaces (1)\ndefault", out)
        self.assertIn("System Namespaces Without NetworkPolicies (1; not part of the verdict)\nkube-system", out)
        self.assertIn("deny-all", out)
        db_line = next(line for line in out.splitlines() if "db-only" in line)
        self.assertIn("matchExpressions", db_line)
        self.assertNotIn("All Pods", db_line)

    def _run_policies(self, fake_run_kubectl_json):
        buf = io.StringIO()
        with (
            mock.patch.object(audit_network_policies, "run_kubectl_json", side_effect=fake_run_kubectl_json),
            contextlib.redirect_stdout(buf),
        ):
            rc = audit_network_policies.audit_network_policies()
        self.assertEqual(rc, 0)
        return buf.getvalue()

    @staticmethod
    def _policies(namespaces, policies):
        def fake_run_kubectl_json(args, env):
            if "networkpolicies" in args:
                return 0, {"items": policies}
            return 0, {"items": [{"metadata": {"name": ns}} for ns in namespaces]}

        return fake_run_kubectl_json

    def test_every_namespace_with_a_restrictive_policy_is_clean(self):
        deny = {"podSelector": {}, "policyTypes": ["Ingress"]}
        out = self._run_policies(self._policies(
            ["prod", "dev"],
            [
                {"metadata": {"namespace": "prod", "name": "deny"}, "spec": deny},
                {"metadata": {"namespace": "dev", "name": "deny"}, "spec": deny},
            ],
        ))
        self.assertIn("CLEAN_AUDIT_STOP: YES", out)
        self.assertIn("enforcement", out)

    def test_allow_all_ingress_policy_is_a_gap(self):
        out = self._run_policies(self._policies(
            ["prod"],
            [{"metadata": {"namespace": "prod", "name": "open"}, "spec": {"podSelector": {}, "ingress": [{}]}}],
        ))
        self.assertIn("1 allow-all-ingress policies", out)
        self.assertIn("ALLOW-ALL INGRESS", out)
        self.assertIn("CLEAN_AUDIT_STOP: NO", out)

    def test_system_namespaces_without_policies_do_not_block_clean(self):
        deny = {"podSelector": {}, "policyTypes": ["Ingress"]}
        out = self._run_policies(self._policies(
            ["prod", "kube-system", "kube-public", "gke-managed-system", "gmp-system"],
            [{"metadata": {"namespace": "prod", "name": "deny"}, "spec": deny}],
        ))
        self.assertIn("CLEAN_AUDIT_STOP: YES", out)
        self.assertIn("System Namespaces Without NetworkPolicies (4", out)

    def test_egress_only_or_partial_selector_namespace_is_a_gap(self):
        out = self._run_policies(self._policies(
            ["egress-only", "partial"],
            [
                {"metadata": {"namespace": "egress-only", "name": "e"}, "spec": {"podSelector": {}, "policyTypes": ["Egress"]}},
                {
                    "metadata": {"namespace": "partial", "name": "db"},
                    "spec": {"podSelector": {"matchLabels": {"tier": "db"}}, "policyTypes": ["Ingress"]},
                },
            ],
        ))
        self.assertIn("2 without full ingress isolation", out)
        self.assertIn("egress-only, partial", out)
        self.assertIn("CLEAN_AUDIT_STOP: NO", out)

    def test_every_source_peers_and_ports_only_rules_allow_all_ingress(self):
        rules = {
            "any-namespace": [{"from": [{"namespaceSelector": {}}]}],
            "any-address": [{"from": [{"ipBlock": {"cidr": "0.0.0.0/0"}}]}],
            "any-address-v6": [{"from": [{"ipBlock": {"cidr": "::/0"}}]}],
            "ports-only": [{"ports": [{"port": 443}]}],
        }
        for name, ingress in rules.items():
            with self.subTest(name):
                out = self._run_policies(self._policies(
                    ["prod"],
                    [{"metadata": {"namespace": "prod", "name": name}, "spec": {"podSelector": {}, "ingress": ingress}}],
                ))
                self.assertIn("1 allow-all-ingress policies", out)
                self.assertIn("CLEAN_AUDIT_STOP: NO", out)

    def test_restricted_peers_do_not_allow_all_ingress(self):
        ingress = [
            {"from": [{"namespaceSelector": {"matchLabels": {"team": "a"}}}]},
            {"from": [{"namespaceSelector": {}, "podSelector": {"matchLabels": {"app": "web"}}}]},
            {"from": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": ["10.0.0.0/8"]}}]},
        ]
        out = self._run_policies(self._policies(
            ["prod"],
            [{"metadata": {"namespace": "prod", "name": "scoped"}, "spec": {"podSelector": {}, "ingress": ingress}}],
        ))
        self.assertIn("CLEAN_AUDIT_STOP: YES", out)

    def test_zero_policies_is_not_clean(self):
        out = self._run_policies(self._policies(["prod", "default"], []))
        self.assertIn("2 namespaces without a NetworkPolicy", out)
        self.assertIn("CLEAN_AUDIT_STOP: NO", out)

    def test_failed_listing_prints_stop_no(self):
        def forbidden_policies(args, env):
            if "networkpolicies" in args:
                return 1, {"error": "Error from server (Forbidden): networkpolicies.networking.k8s.io is forbidden"}
            return 0, {"items": [{"metadata": {"name": "default"}}]}

        out = self._run_policies(forbidden_policies)
        self.assertIn("could not read: networkpolicies", out)
        self.assertIn("CLEAN_AUDIT_STOP: NO", out)
        self.assertNotIn("CLEAN_AUDIT_STOP: YES", out)

    def test_empty_kubectl_output_is_an_error_not_zero_namespaces(self):
        empty = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(audit_network_policies.subprocess, "run", return_value=empty):
            code, data = audit_network_policies.run_kubectl_json(["get", "ns"], {})
        self.assertNotEqual(code, 0)
        self.assertIn("no output", data["error"])


if __name__ == "__main__":
    unittest.main()
