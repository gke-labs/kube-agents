#!/usr/bin/env python3
"""
Unit tests for telemetry_audit.py.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import telemetry_audit


class TestTelemetryAudit(unittest.TestCase):

    def test_find_manifest_path_nested_name(self):
        """Verify find_manifest_path does not falsely match nested name keys."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_content = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: web-app
  namespace: prod
spec:
  template:
    spec:
      containers:
      - name: container-one
        image: nginx
      volumes:
      - name: target-cm
        configMap:
          name: target-cm
"""
            yaml_path = os.path.join(tmpdir, "deployment.yaml")
            with open(yaml_path, "w", encoding="utf-8") as f:
                f.write(yaml_content)

            # Searching for ConfigMap target-cm should NOT match deployment.yaml
            res = telemetry_audit.find_manifest_path(tmpdir, "ConfigMap", "target-cm", "prod")
            self.assertEqual(res, "")

            # Searching for Deployment web-app should match
            res2 = telemetry_audit.find_manifest_path(tmpdir, "Deployment", "web-app", "prod")
            self.assertEqual(res2, "deployment.yaml")

    def test_find_manifest_path_cluster_scoping(self):
        """Verify find_manifest_path scopes matches to the target cluster in multi-cluster repos."""
        with tempfile.TemporaryDirectory() as tmpdir:
            c1_dir = os.path.join(tmpdir, "clusters", "cluster-a", "workloads")
            c2_dir = os.path.join(tmpdir, "clusters", "cluster-b", "workloads")
            os.makedirs(c1_dir, exist_ok=True)
            os.makedirs(c2_dir, exist_ok=True)

            yaml_content = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: frontend-app
  namespace: prod
spec:
  template:
    spec:
      containers:
      - name: frontend
        image: nginx
"""
            p1 = os.path.join(c1_dir, "frontend.yaml")
            p2 = os.path.join(c2_dir, "frontend.yaml")
            with open(p1, "w", encoding="utf-8") as f:
                f.write(yaml_content)
            with open(p2, "w", encoding="utf-8") as f:
                f.write(yaml_content)

            # cluster-a should resolve to cluster-a path
            res_a = telemetry_audit.find_manifest_path(
                tmpdir, "Deployment", "frontend-app", "prod", cluster_name="cluster-a"
            )
            self.assertEqual(res_a, os.path.normpath("clusters/cluster-a/workloads/frontend.yaml"))

            # cluster-b should resolve to cluster-b path
            res_b = telemetry_audit.find_manifest_path(
                tmpdir, "Deployment", "frontend-app", "prod", cluster_name="cluster-b"
            )
            self.assertEqual(res_b, os.path.normpath("clusters/cluster-b/workloads/frontend.yaml"))

            # Unmatched cluster-c should fail closed rather than mutating another cluster
            res_c = telemetry_audit.find_manifest_path(
                tmpdir, "Deployment", "frontend-app", "prod", cluster_name="cluster-c"
            )
            self.assertEqual(res_c, "")

    def test_find_manifest_path_namespace_scoping(self):
        """Verify find_manifest_path disambiguates un-namespaced manifests via overlay paths."""
        with tempfile.TemporaryDirectory() as tmpdir:
            staging_dir = os.path.join(tmpdir, "overlays", "staging")
            prod_dir = os.path.join(tmpdir, "overlays", "prod")
            os.makedirs(staging_dir, exist_ok=True)
            os.makedirs(prod_dir, exist_ok=True)

            # Manifests without explicit metadata.namespace
            yaml_content = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: web-app
spec:
  template:
    spec:
      containers:
      - name: web
        image: nginx
"""
            with open(os.path.join(staging_dir, "web-app.yaml"), "w", encoding="utf-8") as f:
                f.write(yaml_content)
            with open(os.path.join(prod_dir, "web-app.yaml"), "w", encoding="utf-8") as f:
                f.write(yaml_content)

            res_prod = telemetry_audit.find_manifest_path(
                tmpdir, "Deployment", "web-app", "prod"
            )
            self.assertEqual(res_prod, os.path.normpath("overlays/prod/web-app.yaml"))

            res_staging = telemetry_audit.find_manifest_path(
                tmpdir, "Deployment", "web-app", "staging"
            )
            self.assertEqual(res_staging, os.path.normpath("overlays/staging/web-app.yaml"))

            # Unknown namespace should fail closed rather than picking an arbitrary overlay
            res_unknown = telemetry_audit.find_manifest_path(
                tmpdir, "Deployment", "web-app", "dev"
            )
            self.assertEqual(res_unknown, "")

    def test_find_manifest_path_conflicting_namespace_rejected(self):
        """Verify find_manifest_path rejects manifests declaring a different namespace."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_content = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: web-app
  namespace: dev
spec:
  template:
    spec:
      containers:
      - name: web
        image: nginx
"""
            with open(os.path.join(tmpdir, "app.yaml"), "w", encoding="utf-8") as f:
                f.write(yaml_content)

            res = telemetry_audit.find_manifest_path(tmpdir, "Deployment", "web-app", "prod")
            self.assertEqual(res, "")

    @patch.object(telemetry_audit, "run_cmd")
    def test_check_cfs_quota_flags_throttled_container(self, mock_run_cmd):
        deployments_json = json.dumps({
            "items": [{
                "kind": "Deployment",
                "metadata": {"name": "throttled-svc", "namespace": "default"},
                "spec": {
                    "replicas": 1,
                    "template": {
                        "spec": {
                            "containers": [{
                                "name": "app",
                                "resources": {
                                    "limits": {"cpu": "200m"},
                                    "requests": {"cpu": "200m"}
                                }
                            }]
                        }
                    }
                }
            }]
        })
        mock_run_cmd.side_effect = [
            (0, deployments_json, ""),
            (0, deployments_json, ""),
        ]

        env = {"KUBECONFIG": "/opt/data/.kubeconfigs/kc.yaml"}
        findings = []
        checks_run = []
        limitations = []

        telemetry_audit.check_cfs_quota(env, "test-cluster", "", findings, checks_run, limitations)

        self.assertEqual(len(checks_run), 1)
        self.assertEqual(checks_run[0]["check"], "cfs-quota-throttling")
        self.assertIn("KUBECONFIG=/opt/data/.kubeconfigs/kc.yaml", checks_run[0]["command"])
        self.assertNotIn("--context=", checks_run[0]["command"])

        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["check"], "cfs-quota-throttling")
        self.assertEqual(f["object"], "Deployment/throttled-svc")
        self.assertIn("risk", f["impact"])
        self.assertIn("KUBECONFIG=/opt/data/.kubeconfigs/kc.yaml", f["evidence"]["command"])

    @patch.object(telemetry_audit, "run_cmd")
    def test_check_conntrack_autopilot_not_in_checks_run(self, mock_run_cmd):
        """Autopilot clusters must be in checks_not_applicable and NOT in checks_run."""
        nodes_json = json.dumps({
            "items": [{
                "kind": "Node",
                "metadata": {
                    "name": "node-1",
                    "labels": {"cloud.google.com/gke-autopilot": "true"}
                }
            }]
        })
        mock_run_cmd.return_value = (0, nodes_json, "")

        env = {"KUBECONFIG": "/opt/data/.kubeconfigs/kc.yaml"}
        findings = []
        checks_run = []
        limitations = []
        checks_not_applicable = []

        telemetry_audit.check_conntrack(
            env, "auto-cluster", "", findings, checks_run, limitations, checks_not_applicable
        )

        self.assertEqual(len(checks_run), 0)
        self.assertEqual(len(checks_not_applicable), 1)
        self.assertEqual(checks_not_applicable[0]["check"], "conntrack-saturation")
        self.assertEqual(len(findings), 0)

    @patch.object(telemetry_audit, "run_cmd")
    def test_check_conntrack_healthy_default_no_tuning_ds(self, mock_run_cmd):
        """Clusters without tuning DaemonSet are healthy by default."""
        nodes_and_ds = json.dumps({
            "items": [{
                "kind": "Node",
                "metadata": {"name": "node-1", "labels": {}}
            }]
        })
        mock_run_cmd.return_value = (0, nodes_and_ds, "")

        env = {"KUBECONFIG": "/opt/data/.kubeconfigs/kc.yaml"}
        findings = []
        checks_run = []
        limitations = []
        checks_not_applicable = []

        telemetry_audit.check_conntrack(
            env, "standard-cluster", "", findings, checks_run, limitations, checks_not_applicable
        )

        self.assertEqual(len(checks_run), 1)
        self.assertEqual(checks_run[0]["check"], "conntrack-saturation")
        self.assertEqual(len(checks_not_applicable), 0)
        self.assertEqual(len(findings), 0)

    @patch.object(telemetry_audit, "run_cmd")
    def test_check_conntrack_flags_suboptimal_tuning_ds(self, mock_run_cmd):
        """Flags DaemonSet that tunes conntrack below 131072."""
        items_json = json.dumps({
            "items": [
                {
                    "kind": "Node",
                    "metadata": {"name": "node-1", "labels": {}}
                },
                {
                    "kind": "DaemonSet",
                    "metadata": {"name": "custom-tuning", "namespace": "kube-system"},
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [{
                                    "name": "tuner",
                                    "command": ["sysctl", "-w", "net.netfilter.nf_conntrack_max=65536"]
                                }]
                            }
                        }
                    }
                }
            ]
        })
        mock_run_cmd.return_value = (0, items_json, "")

        env = {"KUBECONFIG": "/opt/data/.kubeconfigs/kc.yaml"}
        findings = []
        checks_run = []
        limitations = []
        checks_not_applicable = []

        telemetry_audit.check_conntrack(
            env, "standard-cluster", "", findings, checks_run, limitations, checks_not_applicable
        )

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["object"], "DaemonSet/custom-tuning")
        self.assertIn("65536", findings[0]["title"])

    @patch.object(telemetry_audit, "run_cmd")
    def test_check_ingress_drain_records_limitation_on_read_failure(self, mock_run_cmd):
        """When ingress/gateway read fails, ingress-502-drain records a limitation and returns."""
        svc_json = json.dumps({
            "items": [{
                "kind": "Service",
                "metadata": {"name": "web-svc", "namespace": "default"},
                "spec": {"selector": {"app": "web"}}
            }]
        })
        # svc succeeds, ingress fails with connection error, httproute fails with CRD missing error
        mock_run_cmd.side_effect = [
            (0, svc_json, ""),
            (1, "", "error: connection refused to api-server"),
            (1, "", "error: the server doesn't have a resource type \"httproute\""),
        ]

        env = {"KUBECONFIG": "/opt/data/.kubeconfigs/kc.yaml"}
        findings = []
        checks_run = []
        limitations = []

        telemetry_audit.check_ingress_drain(env, "my-cluster", "", findings, checks_run, limitations)

        self.assertEqual(len(limitations), 1)
        self.assertIn("ingress-502-drain: ingress read failed", limitations[0])
        self.assertEqual(len(findings), 0)

    def test_resolve_workspace_explicit(self):
        """CLI workspace path is prioritized and resolved to absolute path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            res = telemetry_audit.resolve_workspace(tmpdir)
            self.assertEqual(res, os.path.abspath(tmpdir))

    def test_resolve_workspace_env(self):
        """GITOPS_WORKSPACE environment variable is used when CLI arg is absent."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"GITOPS_WORKSPACE": tmpdir}):
                res = telemetry_audit.resolve_workspace(None)
                self.assertEqual(res, os.path.abspath(tmpdir))

    def test_resolve_workspace_lease_discovery(self):
        """Active stream lease under gitops root is discovered when no env or CLI is passed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            stream_dir = os.path.join(tmpdir, telemetry_audit.AUDIT_STREAM_NAME, "owner__repo")
            os.makedirs(stream_dir, exist_ok=True)
            with patch.dict(os.environ, {"FLEET_AUDIT_GITOPS_ROOT": tmpdir}, clear=False):
                if "GITOPS_WORKSPACE" in os.environ:
                    del os.environ["GITOPS_WORKSPACE"]
                res = telemetry_audit.resolve_workspace(None)
                self.assertEqual(res, stream_dir)


if __name__ == "__main__":
    unittest.main()
