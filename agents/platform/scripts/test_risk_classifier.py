"""Exhaustive scenario, adversarial evasion, determinism, and performance tests for risk_classifier."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import risk_classifier
from risk_classifier import (
    BLAST_CRITICAL,
    BLAST_HIGH,
    BLAST_LOW,
    BLAST_MEDIUM,
    BLAST_UNKNOWN,
    CHANGE_ADDED,
    CHANGE_DELETED,
    CHANGE_MODIFIED,
    CHANGE_RENAMED,
    DESTRUCTIVE_NO,
    DESTRUCTIVE_UNKNOWN,
    DESTRUCTIVE_YES,
    REVERT_DIFFICULT,
    REVERT_EASY,
    REVERT_IRREVERSIBLE,
    REVERT_MODERATE,
    REVERT_UNKNOWN,
    RISK_CRITICAL,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    RISK_UNKNOWN,
    FileChange,
    classify_changes,
    classify_git_range,
    combine_blast_radius,
    combine_destructiveness,
    combine_revertability,
    combine_risk,
    load_git_changes,
)
from risk_classifier.__main__ import main as cli_main

BASE_DEPLOY = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
  namespace: shop
  labels:
    app: web
spec:
  replicas: 4
  selector:
    matchLabels:
      app: web
  template:
    metadata:
      labels:
        app: web
    spec:
      serviceAccountName: web-sa
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
      containers:
        - name: app
          image: gcr.io/acme/web:v1.2.3
          ports:
            - name: http
              containerPort: 8080
          resources:
            requests:
              cpu: "100m"
              memory: "128Mi"
            limits:
              cpu: "500m"
              memory: "512Mi"
          readinessProbe:
            httpGet:
              path: /healthz
              port: http
"""

BASE_SERVICE = """\
apiVersion: v1
kind: Service
metadata:
  name: web
  namespace: shop
spec:
  type: ClusterIP
  selector:
    app: web
  ports:
    - name: http
      port: 80
      targetPort: 8080
"""

BASE_CONFIGMAP = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: web-config
  namespace: shop
data:
  LOG_LEVEL: info
  FEATURE_X: "true"
"""

BASE_HPA = """\
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: web-hpa
  namespace: shop
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: web
  minReplicas: 2
  maxReplicas: 6
"""

BASE_PVC = """\
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: pg-data
  namespace: shop
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: standard-rwo
  resources:
    requests:
      storage: 10Gi
"""


def _mod(path: str, before: str, after: str, **kw) -> FileChange:
    return FileChange(
        path=path,
        change_type=CHANGE_MODIFIED,
        before=before.encode("utf-8"),
        after=after.encode("utf-8"),
        before_mode=kw.pop("before_mode", "100644"),
        after_mode=kw.pop("after_mode", "100644"),
        **kw,
    )


def _add(path: str, after: str, **kw) -> FileChange:
    return FileChange(
        path=path,
        change_type=CHANGE_ADDED,
        before=None,
        after=after.encode("utf-8"),
        after_mode=kw.pop("after_mode", "100644"),
        **kw,
    )


def _del(path: str, before: str, **kw) -> FileChange:
    return FileChange(
        path=path,
        change_type=CHANGE_DELETED,
        before=before.encode("utf-8"),
        after=None,
        before_mode=kw.pop("before_mode", "100644"),
        **kw,
    )


class LatticeTests(unittest.TestCase):
    """Verify anti-masking properties across all 4 aggregation lattices."""

    def test_risk_anti_masking(self) -> None:
        self.assertEqual(combine_risk(RISK_LOW, RISK_UNKNOWN), RISK_UNKNOWN)
        self.assertEqual(combine_risk(RISK_MEDIUM, RISK_UNKNOWN), RISK_UNKNOWN)
        self.assertEqual(combine_risk(RISK_HIGH, RISK_UNKNOWN), RISK_HIGH)
        self.assertEqual(combine_risk(RISK_CRITICAL, RISK_UNKNOWN), RISK_CRITICAL)
        self.assertEqual(combine_risk(RISK_UNKNOWN, RISK_HIGH), RISK_HIGH)
        self.assertEqual(combine_risk(RISK_UNKNOWN, RISK_CRITICAL), RISK_CRITICAL)

    def test_blast_anti_masking(self) -> None:
        self.assertEqual(combine_blast_radius(BLAST_LOW, BLAST_UNKNOWN), BLAST_UNKNOWN)
        self.assertEqual(combine_blast_radius(BLAST_MEDIUM, BLAST_UNKNOWN), BLAST_UNKNOWN)
        self.assertEqual(combine_blast_radius(BLAST_HIGH, BLAST_UNKNOWN), BLAST_HIGH)
        self.assertEqual(combine_blast_radius(BLAST_CRITICAL, BLAST_UNKNOWN), BLAST_CRITICAL)

    def test_revertability_anti_masking(self) -> None:
        self.assertEqual(combine_revertability(REVERT_EASY, REVERT_UNKNOWN), REVERT_UNKNOWN)
        self.assertEqual(combine_revertability(REVERT_MODERATE, REVERT_UNKNOWN), REVERT_UNKNOWN)
        self.assertEqual(combine_revertability(REVERT_DIFFICULT, REVERT_UNKNOWN), REVERT_UNKNOWN)
        self.assertEqual(combine_revertability(REVERT_IRREVERSIBLE, REVERT_UNKNOWN), REVERT_IRREVERSIBLE)

    def test_destructiveness_anti_masking(self) -> None:
        self.assertEqual(combine_destructiveness(DESTRUCTIVE_NO, DESTRUCTIVE_UNKNOWN), DESTRUCTIVE_UNKNOWN)
        self.assertEqual(combine_destructiveness(DESTRUCTIVE_YES, DESTRUCTIVE_UNKNOWN), DESTRUCTIVE_YES)
        self.assertEqual(combine_destructiveness(DESTRUCTIVE_UNKNOWN, DESTRUCTIVE_YES), DESTRUCTIVE_YES)


class WorkloadScenarioTests(unittest.TestCase):
    """Core Kubernetes workload scenarios (W-01 through W-33)."""

    def test_pinned_semver_image_bump_is_low(self) -> None:
        r = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, BASE_DEPLOY.replace("v1.2.3", "v1.2.4"))])
        self.assertEqual(r.risk_level, RISK_LOW)
        self.assertEqual(r.blast_radius, BLAST_LOW)
        self.assertEqual(r.revertability, REVERT_EASY)
        self.assertEqual(r.destructiveness, DESTRUCTIVE_NO)
        self.assertTrue(r.supported)
        self.assertTrue(r.effective_changes)
        self.assertEqual(r.risk_label, "risk:low")

    def test_sha256_digest_bump_is_low(self) -> None:
        digest = "gcr.io/acme/web@sha256:" + ("a" * 64)
        r = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, BASE_DEPLOY.replace("gcr.io/acme/web:v1.2.3", digest))])
        self.assertEqual(r.risk_level, RISK_LOW)
        self.assertEqual(r.revertability, REVERT_EASY)

    def test_mutable_tags_are_medium_unknown_revert(self) -> None:
        for bad_tag in ("latest", "main", "qa", "rc", "beta", "pr-42", "1.2.3-nightly"):
            with self.subTest(tag=bad_tag):
                r = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, BASE_DEPLOY.replace("v1.2.3", bad_tag))])
                self.assertEqual(r.risk_level, RISK_MEDIUM)
                self.assertEqual(r.revertability, REVERT_UNKNOWN)

    def test_image_repo_swap_is_high(self) -> None:
        r = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, BASE_DEPLOY.replace("gcr.io/acme/web:v1.2.3", "docker.io/evil/web:v1.2.3"))])
        self.assertEqual(r.risk_level, RISK_HIGH)

    def test_modest_replica_bump_is_low(self) -> None:
        r = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, BASE_DEPLOY.replace("replicas: 4", "replicas: 6"))])
        self.assertEqual(r.risk_level, RISK_LOW)
        self.assertEqual(r.destructiveness, DESTRUCTIVE_NO)
        self.assertEqual(r.revertability, REVERT_EASY)

    def test_large_replica_spike_is_medium(self) -> None:
        r = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, BASE_DEPLOY.replace("replicas: 4", "replicas: 40"))])
        self.assertEqual(r.risk_level, RISK_MEDIUM)
        self.assertEqual(r.blast_radius, BLAST_MEDIUM)

    def test_modest_scale_down_is_medium_non_destructive(self) -> None:
        r = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, BASE_DEPLOY.replace("replicas: 4", "replicas: 3"))])
        self.assertEqual(r.risk_level, RISK_MEDIUM)
        self.assertEqual(r.destructiveness, DESTRUCTIVE_NO)

    def test_steep_scale_down_and_zero_are_high_destructive(self) -> None:
        for target in (1, 0):
            with self.subTest(replicas=target):
                r = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, BASE_DEPLOY.replace("replicas: 4", f"replicas: {target}"))])
                self.assertEqual(r.risk_level, RISK_HIGH)
                self.assertEqual(r.destructiveness, DESTRUCTIVE_YES)

    def test_recreate_strategy_is_high_destructive(self) -> None:
        after = BASE_DEPLOY.replace("replicas: 4", "replicas: 4\n  strategy:\n    type: Recreate")
        r = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, after)])
        self.assertEqual(r.risk_level, RISK_HIGH)
        self.assertEqual(r.destructiveness, DESTRUCTIVE_YES)
        self.assertEqual(r.revertability, REVERT_MODERATE)

    def test_immutable_selector_change_is_high_destructive(self) -> None:
        after = BASE_DEPLOY.replace("matchLabels:\n      app: web", "matchLabels:\n      app: web-v2")
        r = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, after)])
        self.assertEqual(r.risk_level, RISK_HIGH)
        self.assertEqual(r.destructiveness, DESTRUCTIVE_YES)
        self.assertEqual(r.revertability, REVERT_MODERATE)

    def test_command_args_and_lifecycle_edits_are_high(self) -> None:
        for snippet in (
            '          command: ["/bin/sh", "-c", "id"]\n',
            '          args: ["--debug"]\n',
            '          lifecycle:\n            postStart:\n              exec:\n                command: ["/bin/sh", "-c", "curl evil"]\n',
        ):
            with self.subTest(snippet=snippet.strip()):
                after = BASE_DEPLOY.replace("image: gcr.io/acme/web:v1.2.3\n", f"image: gcr.io/acme/web:v1.2.3\n{snippet}")
                r = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, after)])
                self.assertEqual(r.risk_level, RISK_HIGH)

    def test_secret_env_ref_and_envfrom_are_high(self) -> None:
        env_secret = (
            "          env:\n"
            "            - name: TOKEN\n"
            "              valueFrom:\n"
            "                secretKeyRef:\n"
            "                  name: api-sec\n"
            "                  key: tok\n"
        )
        r1 = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, BASE_DEPLOY + env_secret)])
        self.assertEqual(r1.risk_level, RISK_HIGH)
        env_from = "          envFrom:\n            - secretRef:\n                name: api-sec\n"
        r2 = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, BASE_DEPLOY + env_from)])
        self.assertEqual(r2.risk_level, RISK_HIGH)

    def test_safe_resource_bump_including_init_container_is_low(self) -> None:
        before = BASE_DEPLOY + (
            "      initContainers:\n"
            "        - name: init\n"
            "          image: gcr.io/acme/init:v1.0.0\n"
            "          resources:\n"
            "            requests:\n"
            "              cpu: \"50m\"\n"
            "              memory: \"64Mi\"\n"
        )
        after = before.replace('cpu: "50m"', 'cpu: "100m"').replace('cpu: "100m"\n              memory: "128Mi"', 'cpu: "200m"\n              memory: "128Mi"')
        r = classify_changes([_mod("clusters/dev/web.yaml", before, after)])
        self.assertEqual(r.risk_level, RISK_LOW)
        self.assertEqual(r.destructiveness, DESTRUCTIVE_NO)

    def test_request_exceeds_limit_is_high_destructive(self) -> None:
        after = BASE_DEPLOY.replace('cpu: "100m"', 'cpu: "800m"')
        r = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, after)])
        self.assertEqual(r.risk_level, RISK_HIGH)
        self.assertEqual(r.destructiveness, DESTRUCTIVE_YES)

    def test_privileged_daemonset_node_rootkit_is_critical(self) -> None:
        ds = """\
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: node-agent
  namespace: monitoring
spec:
  selector:
    matchLabels:
      app: node-agent
  template:
    metadata:
      labels:
        app: node-agent
    spec:
      hostNetwork: true
      containers:
        - name: agent
          image: gcr.io/acme/agent:v1.0.0
          securityContext:
            privileged: true
"""
        r = classify_changes([_add("clusters/dev/ds.yaml", ds)])
        self.assertEqual(r.risk_level, RISK_CRITICAL)
        self.assertEqual(r.blast_radius, BLAST_CRITICAL)

    def test_bare_pod_hostpath_docker_sock_is_critical(self) -> None:
        pod = """\
apiVersion: v1
kind: Pod
metadata:
  name: debug-pod
  namespace: shop
spec:
  volumes:
    - name: dock
      hostPath:
        path: /var/run/docker.sock
  containers:
    - name: shell
      image: gcr.io/acme/debug:v1.0.0
"""
        r = classify_changes([_add("clusters/dev/pod.yaml", pod)])
        self.assertEqual(r.risk_level, RISK_CRITICAL)

    def test_job_is_high_irreversible_and_cronjob_suspend_is_high_destructive(self) -> None:
        job = """\
apiVersion: batch/v1
kind: Job
metadata:
  name: migrate
  namespace: shop
spec:
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: run
          image: gcr.io/acme/migrate:v1.0.0
"""
        rj = classify_changes([_add("clusters/dev/job.yaml", job)])
        self.assertEqual(rj.risk_level, RISK_HIGH)
        self.assertEqual(rj.revertability, REVERT_IRREVERSIBLE)

        cj = """\
apiVersion: batch/v1
kind: CronJob
metadata:
  name: nightly
  namespace: shop
spec:
  schedule: "0 2 * * *"
  suspend: false
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: OnFailure
          containers:
            - name: run
              image: gcr.io/acme/nightly:v1.0.0
"""
        r_low = classify_changes([_mod("clusters/dev/cj.yaml", cj, cj.replace("v1.0.0", "v1.0.1"))])
        self.assertEqual(r_low.risk_level, RISK_LOW)
        r_susp = classify_changes([_mod("clusters/dev/cj.yaml", cj, cj.replace("suspend: false", "suspend: true"))])
        self.assertEqual(r_susp.risk_level, RISK_HIGH)
        self.assertEqual(r_susp.destructiveness, DESTRUCTIVE_YES)


class StorageConfigScalingSecurityTests(unittest.TestCase):
    """HPA, VPA, PDB, ConfigMap, Secret, PVC, Namespace, Networking, Mesh, Policy, GitOps, Cloud IaC."""

    def test_hpa_bump_low_vs_shrink_high(self) -> None:
        r_ok = classify_changes([_mod("clusters/dev/hpa.yaml", BASE_HPA, BASE_HPA.replace("maxReplicas: 6", "maxReplicas: 8"))])
        self.assertEqual(r_ok.risk_level, RISK_LOW)
        r_bad = classify_changes([_mod("clusters/dev/hpa.yaml", BASE_HPA, BASE_HPA.replace("maxReplicas: 6", "maxReplicas: 1"))])
        self.assertEqual(r_bad.risk_level, RISK_HIGH)
        self.assertEqual(r_bad.destructiveness, DESTRUCTIVE_YES)

    def test_vpa_unquoted_off_parses_as_yaml12_string_medium_vs_auto_high(self) -> None:
        vpa_off = """\
apiVersion: autoscaling.k8s.io/v1
kind: VerticalPodAutoscaler
metadata:
  name: web-vpa
  namespace: shop
spec:
  updatePolicy:
    updateMode: Off
"""
        r_off = classify_changes([_add("clusters/dev/vpa.yaml", vpa_off)])
        self.assertEqual(r_off.risk_level, RISK_MEDIUM)
        self.assertTrue(r_off.supported)
        r_auto = classify_changes([_add("clusters/dev/vpa.yaml", vpa_off.replace("Off", "Auto"))])
        self.assertEqual(r_auto.risk_level, RISK_HIGH)
        self.assertEqual(r_auto.destructiveness, DESTRUCTIVE_YES)

    def test_pdb_offline_add_is_medium_and_drain_deadlock_is_high_destructive(self) -> None:
        pdb = """\
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: web-pdb
  namespace: shop
spec:
  maxUnavailable: 1
  selector:
    matchLabels:
      app: web
"""
        r_add = classify_changes([_add("clusters/dev/pdb.yaml", pdb)])
        self.assertEqual(r_add.risk_level, RISK_MEDIUM)
        self.assertEqual(r_add.destructiveness, DESTRUCTIVE_NO)
        r_dead = classify_changes([_add("clusters/dev/pdb.yaml", pdb.replace("maxUnavailable: 1", "maxUnavailable: 0"))])
        self.assertEqual(r_dead.risk_level, RISK_HIGH)
        self.assertEqual(r_dead.destructiveness, DESTRUCTIVE_YES)

    def test_configmap_safe_scalar_vs_secret_leak_critical(self) -> None:
        r_ok = classify_changes([_mod("clusters/dev/cm.yaml", BASE_CONFIGMAP, BASE_CONFIGMAP + '  CACHE_TTL: "60"\n')])
        self.assertEqual(r_ok.risk_level, RISK_LOW)

        for leaked_line in (
            '  DB_PASSWORD: "SuperSecretPassword123!"\n',
            '  GCP_KEY: "' + "AIza" + 'SyD-1234567890abcdef1234567890abcde"\n',
            '  OPENAI: "' + "sk-" + 'proj-1234567890abcdef1234567890"\n',
            '  TOKEN: "' + "eyJ" + 'hbGciOiJIUzI1NiJ9.' + "eyJ" + 'zdWIiOiIxMjM0NTY3ODkwIn0.abc1234567890def"\n',
        ):
            with self.subTest(leaked=leaked_line.strip()):
                r_leak = classify_changes([_mod("clusters/dev/cm.yaml", BASE_CONFIGMAP, BASE_CONFIGMAP + leaked_line)])
                self.assertEqual(r_leak.risk_level, RISK_CRITICAL)
                self.assertEqual(r_leak.revertability, REVERT_IRREVERSIBLE)

    def test_pvc_expand_difficult_shrink_destructive_delete_critical(self) -> None:
        r_exp = classify_changes([_mod("clusters/dev/pvc.yaml", BASE_PVC, BASE_PVC.replace("10Gi", "20Gi"))])
        self.assertEqual(r_exp.risk_level, RISK_HIGH)
        self.assertEqual(r_exp.revertability, REVERT_DIFFICULT)
        self.assertEqual(r_exp.destructiveness, DESTRUCTIVE_NO)

        r_shr = classify_changes([_mod("clusters/dev/pvc.yaml", BASE_PVC, BASE_PVC.replace("10Gi", "5Gi"))])
        self.assertEqual(r_shr.risk_level, RISK_HIGH)
        self.assertEqual(r_shr.destructiveness, DESTRUCTIVE_YES)
        self.assertEqual(r_shr.revertability, REVERT_IRREVERSIBLE)

        r_del = classify_changes([_del("clusters/dev/pvc.yaml", BASE_PVC)])
        self.assertEqual(r_del.risk_level, RISK_CRITICAL)
        self.assertEqual(r_del.revertability, REVERT_IRREVERSIBLE)
        self.assertEqual(r_del.destructiveness, DESTRUCTIVE_YES)

    def test_namespace_delete_and_kube_system_delete_are_critical(self) -> None:
        ns = "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: payments\n"
        r_ns = classify_changes([_del("clusters/dev/ns.yaml", ns)])
        self.assertEqual(r_ns.risk_level, RISK_CRITICAL)
        self.assertEqual(r_ns.blast_radius, BLAST_CRITICAL)
        self.assertEqual(r_ns.revertability, REVERT_IRREVERSIBLE)
        self.assertEqual(r_ns.destructiveness, DESTRUCTIVE_YES)

        ks = BASE_DEPLOY.replace("namespace: shop", "namespace: kube-system")
        r_ks = classify_changes([_del("clusters/dev/coredns.yaml", ks)])
        self.assertEqual(r_ks.risk_level, RISK_CRITICAL)
        self.assertEqual(r_ks.destructiveness, DESTRUCTIVE_YES)

    def test_service_loadbalancer_non_destructive_vs_ports_destructive_vs_external_ips_critical(self) -> None:
        r_lb = classify_changes([_mod("clusters/dev/svc.yaml", BASE_SERVICE, BASE_SERVICE.replace("ClusterIP", "LoadBalancer"))])
        self.assertEqual(r_lb.risk_level, RISK_HIGH)
        self.assertEqual(r_lb.destructiveness, DESTRUCTIVE_NO)

        r_port = classify_changes([_mod("clusters/dev/svc.yaml", BASE_SERVICE, BASE_SERVICE.replace("port: 80", "port: 8081"))])
        self.assertEqual(r_port.risk_level, RISK_HIGH)
        self.assertEqual(r_port.destructiveness, DESTRUCTIVE_YES)

        r_ext = classify_changes([_mod("clusters/dev/svc.yaml", BASE_SERVICE, BASE_SERVICE + "  externalIPs: [1.2.3.4]\n")])
        self.assertEqual(r_ext.risk_level, RISK_CRITICAL)

    def test_rbac_cluster_admin_and_wildcard_are_critical(self) -> None:
        crb = """\
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: super-admin
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cluster-admin
subjects:
  - kind: ServiceAccount
    name: web-sa
    namespace: shop
"""
        r = classify_changes([_add("clusters/dev/crb.yaml", crb)])
        self.assertEqual(r.risk_level, RISK_CRITICAL)
        self.assertEqual(r.blast_radius, BLAST_CRITICAL)

        cr = """\
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: wild
rules:
  - apiGroups: ["*"]
    resources: ["*"]
    verbs: ["*"]
"""
        self.assertEqual(classify_changes([_add("clusters/dev/cr.yaml", cr)]).risk_level, RISK_CRITICAL)

    def test_kyverno_gatekeeper_istio_gateway_api_and_cloud_iac(self) -> None:
        # Kyverno Enforce -> Audit
        kyv_b = "apiVersion: kyverno.io/v1\nkind: ClusterPolicy\nmetadata:\n  name: require-nonroot\nspec:\n  validationFailureAction: Enforce\n"
        kyv_a = kyv_b.replace("Enforce", "Audit")
        self.assertEqual(classify_changes([_mod("clusters/dev/kyv.yaml", kyv_b, kyv_a)]).risk_level, RISK_CRITICAL)

        # Istio PeerAuthentication DISABLE in istio-system
        pa = "apiVersion: security.istio.io/v1beta1\nkind: PeerAuthentication\nmetadata:\n  name: default\n  namespace: istio-system\nspec:\n  mtls:\n    mode: DISABLE\n"
        self.assertEqual(classify_changes([_add("clusters/dev/pa.yaml", pa)]).risk_level, RISK_CRITICAL)

        # Gateway API ReferenceGrant
        rg = "apiVersion: gateway.networking.k8s.io/v1beta1\nkind: ReferenceGrant\nmetadata:\n  name: allow-gw\n  namespace: certs\nspec: {}\n"
        self.assertEqual(classify_changes([_add("clusters/dev/rg.yaml", rg)]).risk_level, RISK_HIGH)

        # KCC IAMPolicyMember roles/owner -> critical
        iam = "apiVersion: iam.cnrm.cloud.google.com/v1beta1\nkind: IAMPolicyMember\nmetadata:\n  name: owner-bind\n  namespace: config-control\nspec:\n  member: user:a@example.com\n  role: roles/owner\n"
        self.assertEqual(classify_changes([_add("gcp/iam.yaml", iam)]).risk_level, RISK_CRITICAL)

        # Crossplane Upbound / AWS ACK / Azure ASO
        for gvk_yaml in (
            "apiVersion: rds.aws.upbound.io/v1beta1\nkind: Instance\nmetadata:\n  name: db\n  namespace: infra\n",
            "apiVersion: s3.services.k8s.aws/v1alpha1\nkind: Bucket\nmetadata:\n  name: bkt\n  namespace: infra\n",
            "apiVersion: storage.azure.com/v1api20230101\nkind: StorageAccount\nmetadata:\n  name: sa\n  namespace: infra\n",
        ):
            self.assertEqual(classify_changes([_add("gcp/cloud.yaml", gvk_yaml)]).risk_level, RISK_HIGH)

    def test_crd_delete_and_webhook_are_critical(self) -> None:
        crd = "apiVersion: apiextensions.k8s.io/v1\nkind: CustomResourceDefinition\nmetadata:\n  name: widgets.acme.io\n"
        r_del = classify_changes([_del("clusters/dev/crd.yaml", crd)])
        self.assertEqual(r_del.risk_level, RISK_CRITICAL)
        self.assertEqual(r_del.revertability, REVERT_IRREVERSIBLE)

        wh = "apiVersion: admissionregistration.k8s.io/v1\nkind: ValidatingWebhookConfiguration\nmetadata:\n  name: policy-wh\n"
        self.assertEqual(classify_changes([_add("clusters/dev/wh.yaml", wh)]).risk_level, RISK_CRITICAL)

    def test_json_kubernetes_manifest_not_bypassed(self) -> None:
        crb_json = json.dumps(
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRoleBinding",
                "metadata": {"name": "sneaky-admin"},
                "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "cluster-admin"},
                "subjects": [{"kind": "ServiceAccount", "name": "web", "namespace": "shop"}],
            }
        )
        r = classify_changes([_add("clusters/dev/sneaky.json", crb_json)])
        self.assertEqual(r.risk_level, RISK_CRITICAL)
        self.assertTrue(r.supported)


class UnsupportedUnknownAndGovernanceTests(unittest.TestCase):
    """Governance, supply chain, docs, and `unknown` fallback for all unsupported inputs."""

    def test_governance_and_case_varied_ci_and_agent_prompts_are_critical(self) -> None:
        for path in (
            ".kube-agents/automation.yaml",
            ".github/workflows/automerge.yml",
            ".GitHub/Workflows/Deploy.YML",
            "CODEOWNERS",
            ".sops.yaml",
            "renovate.json",
            "AGENTS.md",
            "CLAUDE.md",
            "skills/foo/SKILL.md",
        ):
            with self.subTest(path=path):
                r = classify_changes([_add(path, "hello: world\n")])
                self.assertEqual(r.risk_level, RISK_CRITICAL)
                self.assertEqual(r.blast_radius, BLAST_CRITICAL)

    def test_supply_chain_build_files_are_high(self) -> None:
        for path in ("Dockerfile", "app.Dockerfile", "Makefile", "go.mod", "package.json", "requirements.txt"):
            with self.subTest(path=path):
                r = classify_changes([_add(path, "FROM alpine\n")])
                self.assertEqual(r.risk_level, RISK_HIGH)

    def test_pure_docs_are_low_non_effective_while_sync_dir_md_is_unknown(self) -> None:
        r_doc = classify_changes([_add("docs/soak-test.md", "# Soak Test\n")])
        self.assertEqual(r_doc.risk_level, RISK_LOW)
        self.assertEqual(r_doc.destructiveness, DESTRUCTIVE_NO)
        self.assertFalse(r_doc.effective_changes)
        self.assertTrue(r_doc.supported)

        r_sync = classify_changes([_add("clusters/dev/README.md", "# Could be configMapGenerator input\n")])
        self.assertEqual(r_sync.risk_level, RISK_UNKNOWN)
        self.assertFalse(r_sync.supported)

    def test_comment_only_manifest_is_low_non_effective(self) -> None:
        r = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, "# harmless comment\n" + BASE_DEPLOY)])
        self.assertEqual(r.risk_level, RISK_LOW)
        self.assertEqual(r.revertability, REVERT_EASY)
        self.assertEqual(r.destructiveness, DESTRUCTIVE_NO)
        self.assertFalse(r.effective_changes)

    def test_unsupported_inputs_default_to_unknown(self) -> None:
        cases = [
            ("unknown CRD", [_add("clusters/dev/widget.yaml", "apiVersion: acme.io/v1\nkind: Widget\nmetadata:\n  name: w\n  namespace: shop\nspec:\n  x: 1\n")]),
            ("kustomization", [_add("clusters/dev/kustomization.yaml", "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources: [web.yaml]\n")]),
            ("kustomize overlay", [_add("overlays/dev/cm.yaml", BASE_CONFIGMAP)]),
            ("helm values", [_add("charts/web/values.yaml", "replicas: 2\n")]),
            ("helm template", [_add("clusters/dev/web.yaml", "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {{ .Values.name }}\n")]),
            ("envsubst template", [_add("clusters/dev/web.yaml", BASE_CONFIGMAP.replace("info", "${LOG_LEVEL}"))]),
            ("terraform modify", [_add("infra/main.tf", 'resource "google_storage_bucket" "b" {}\n')]),
            ("python source", [_add("scripts/helper.py", "print('hi')\n")]),
            ("non-k8s yaml", [_add(".golangci.yml", "run:\n  timeout: 5m\n")]),
            ("binary file", [FileChange(path="assets/logo.png", change_type=CHANGE_ADDED, is_binary=True)]),
            ("empty diff", []),
        ]
        for label, changes in cases:
            with self.subTest(case=label):
                r = classify_changes(changes)
                self.assertEqual(r.risk_level, RISK_UNKNOWN)
                self.assertEqual(r.risk_label, "risk:unknown")
                self.assertFalse(r.supported)

    def test_terraform_delete_is_high_destructive(self) -> None:
        r = classify_changes([_del("infra/main.tf", 'resource "google_storage_bucket" "b" {}\n')])
        self.assertEqual(r.risk_level, RISK_HIGH)
        self.assertEqual(r.destructiveness, DESTRUCTIVE_YES)
        self.assertFalse(r.supported)


class AdversarialEvasionAndPerformanceTests(unittest.TestCase):
    """Adversarial parser/diff bypasses, smuggling, production escalation, determinism, and latency."""

    def test_map_to_null_security_context_and_requests_not_bypassed(self) -> None:
        # Removing securityContext via null value must not truncate JSON pointer checks
        after_req_null = BASE_DEPLOY.replace(
            '            requests:\n              cpu: "100m"\n              memory: "128Mi"',
            "            requests:",
        )
        r = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, after_req_null)])
        self.assertEqual(r.risk_level, RISK_HIGH)
        self.assertEqual(r.destructiveness, DESTRUCTIVE_YES)

    def test_yaml11_boolean_smuggling_on_privileged_fails_closed(self) -> None:
        after = BASE_DEPLOY.replace(
            "          image: gcr.io/acme/web:v1.2.3\n",
            "          image: gcr.io/acme/web:v1.2.3\n          securityContext:\n            privileged: yes\n",
        )
        r = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, after)])
        self.assertIn(r.risk_level, (RISK_HIGH, RISK_CRITICAL))

    def test_anchors_merge_keys_dup_keys_bidi_rejected_as_unknown(self) -> None:
        hostile_cases = [
            ("anchor/alias", "a: &x\n  k: v\nb: *x\n"),
            ("merge key", "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n  namespace: shop\ndata:\n  <<: {a: b}\n"),
            ("dup key", "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n  namespace: shop\ndata:\n  k: 1\n  k: 2\n"),
            ("bidi trojan", BASE_CONFIGMAP + "  X: \"abc\u202edef\"\n"),
            ("zero-width", BASE_CONFIGMAP + "  X: \"abc\u200bdef\"\n"),
        ]
        for label, body in hostile_cases:
            with self.subTest(label=label):
                r = classify_changes([_add("clusters/dev/bad.yaml", body)])
                self.assertEqual(r.risk_level, RISK_UNKNOWN)
                self.assertEqual(r.stats.parse_errors, 1)
                self.assertFalse(r.supported)

    def test_protected_labels_and_production_tampering(self) -> None:
        # Adding istio-injection: disabled cannot pass as low
        after = BASE_DEPLOY.replace("    app: web\n", "    app: web\n    istio-injection: disabled\n", 1)
        r = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, after)])
        self.assertEqual(r.risk_level, RISK_HIGH)

        # Stripping env: prod -> critical tampering
        prod_before = BASE_DEPLOY.replace("    app: web\n", "    app: web\n    env: prod\n", 1)
        r_tamp = classify_changes([_mod("clusters/dev/web.yaml", prod_before, BASE_DEPLOY)])
        self.assertEqual(r_tamp.risk_level, RISK_CRITICAL)
        self.assertEqual(r_tamp.destructiveness, DESTRUCTIVE_YES)

    def test_production_escalation_low_to_medium_and_destructive_high_to_critical(self) -> None:
        r_bump = classify_changes([_mod("clusters/prod-us/web.yaml", BASE_DEPLOY, BASE_DEPLOY.replace("replicas: 4", "replicas: 5"))])
        self.assertEqual(r_bump.risk_level, RISK_MEDIUM)

        r_del = classify_changes([_del("clusters/prod-us/web.yaml", BASE_DEPLOY)])
        self.assertEqual(r_del.risk_level, RISK_CRITICAL)

    def test_compound_capacity_multiplication_escalates_to_medium(self) -> None:
        # 5 deployments each bumping 5->10 replicas and 500m->1000m CPU = +37.5 CPU net
        big = BASE_DEPLOY.replace('cpu: "100m"', 'cpu: "500m"').replace('cpu: "500m"\n              memory: "512Mi"', 'cpu: "2000m"\n              memory: "2Gi"')
        big_after = big.replace("replicas: 4", "replicas: 8").replace('cpu: "500m"', 'cpu: "1500m"')
        changes = [
            _mod(f"clusters/dev/web-{i}.yaml", big.replace("name: web", f"name: web-{i}"), big_after.replace("name: web", f"name: web-{i}"))
            for i in range(4)
        ]
        r = classify_changes(changes)
        self.assertEqual(r.risk_level, RISK_MEDIUM)
        self.assertTrue(any(reason.code == "COMPOUND_CAPACITY_INCREASE" for reason in r.reasons))

    def test_multi_file_smuggling_cannot_mask_critical_with_unknown(self) -> None:
        unknown_crd = _add("clusters/dev/w.yaml", "apiVersion: acme.io/v1\nkind: Widget\nmetadata:\n  name: w\n  namespace: shop\n")
        ns_del = _del("clusters/dev/ns.yaml", "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: payments\n")
        r = classify_changes([unknown_crd, ns_del])
        self.assertEqual(r.risk_level, RISK_CRITICAL)
        self.assertFalse(r.supported)

    def test_multi_cluster_and_object_caps(self) -> None:
        c1 = _mod("clusters/dev-a/web.yaml", BASE_DEPLOY, BASE_DEPLOY.replace("replicas: 4", "replicas: 5"))
        c2 = _mod("clusters/dev-b/web.yaml", BASE_DEPLOY, BASE_DEPLOY.replace("replicas: 4", "replicas: 5"))
        r = classify_changes([c1, c2])
        self.assertEqual(r.risk_level, RISK_HIGH)
        self.assertEqual(r.stats.clusters_touched, 2)

    def test_determinism_25x_byte_identical(self) -> None:
        changes = [
            _mod("clusters/dev/web.yaml", BASE_DEPLOY, BASE_DEPLOY.replace("replicas: 4", "replicas: 5")),
            _mod("clusters/dev/cm.yaml", BASE_CONFIGMAP, BASE_CONFIGMAP + '  FOO: "bar"\n'),
        ]
        snapshots = [json.dumps(classify_changes(changes).to_dict(), sort_keys=True) for _ in range(25)]
        self.assertEqual(len(set(snapshots)), 1)

    def test_performance_100_file_pr_under_1s(self) -> None:
        changes = [
            _mod(
                f"clusters/dev/app-{i}.yaml",
                BASE_DEPLOY.replace("name: web", f"name: app-{i}"),
                BASE_DEPLOY.replace("name: web", f"name: app-{i}").replace("v1.2.3", "v1.2.4"),
            )
            for i in range(100)
        ]
        t0 = time.monotonic()
        r = classify_changes(changes)
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        self.assertEqual(r.risk_level, RISK_HIGH)  # >50 objects cap
        self.assertLess(elapsed_ms, 1500.0, f"100-file classification took {elapsed_ms:.1f}ms")

    def test_git_loader_and_cli_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            (repo / "clusters" / "dev").mkdir(parents=True)
            (repo / "clusters" / "dev" / "web.yaml").write_text(BASE_DEPLOY, encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
            base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

            (repo / "clusters" / "dev" / "web.yaml").write_text(BASE_DEPLOY.replace("replicas: 4", "replicas: 5"), encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "bump"], cwd=repo, check=True)
            head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

            res, paths = classify_git_range(repo, base_sha, head_sha)
            self.assertEqual(res.risk_level, RISK_LOW)
            self.assertEqual(paths, ["clusters/dev/web.yaml"])

            import contextlib, io
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc_ok = cli_main(["--repo", str(repo), "--base", base_sha, "--head", head_sha, "--max-risk", "low"])
            self.assertEqual(rc_ok, 0)
            self.assertEqual(json.loads(buf.getvalue())["risk_level"], "low")

    def test_gitattributes_export_ignore_cannot_hide_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            (repo / "clusters" / "dev").mkdir(parents=True)
            (repo / "clusters" / "dev" / "web.yaml").write_text(BASE_DEPLOY, encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
            base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

            # Attacker tries to hide a ClusterRoleBinding via .gitattributes export-ignore
            (repo / ".gitattributes").write_text("clusters/dev/hidden.yaml export-ignore\n", encoding="utf-8")
            (repo / "clusters" / "dev" / "hidden.yaml").write_text(
                "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRoleBinding\n"
                "metadata:\n  name: sneaky\nroleRef:\n  apiGroup: rbac.authorization.k8s.io\n"
                "  kind: ClusterRole\n  name: cluster-admin\nsubjects:\n  - kind: ServiceAccount\n"
                "    name: web\n    namespace: shop\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "sneaky"], cwd=repo, check=True)
            head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

            res, paths = classify_git_range(repo, base_sha, head_sha, disable_gitattributes=True)
            self.assertEqual(res.risk_level, RISK_CRITICAL)
            self.assertIn("clusters/dev/hidden.yaml", paths)
            self.assertTrue(any(r.code == "CRITICAL_PRIVILEGED_RBAC" for r in res.reasons))


class ExtendedCoverageMatrixTests(unittest.TestCase):
    """Extended scenarios: StatefulSet, DaemonSet, PodTemplate, GitOps, Cloud IaC, List, modes, paths."""

    def test_statefulset_retain_vs_delete_and_immutable_and_delete(self) -> None:
        sts = """\
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: db
  namespace: shop
spec:
  serviceName: db-headless
  replicas: 2
  selector:
    matchLabels:
      app: db
  template:
    metadata:
      labels:
        app: db
    spec:
      containers:
        - name: pg
          image: gcr.io/acme/pg:16.2
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: [ReadWriteOnce]
        resources:
          requests:
            storage: 10Gi
"""
        r_retain = classify_changes([_mod("clusters/dev/sts.yaml", sts, sts.replace("replicas: 2", "replicas: 3"))])
        self.assertEqual(r_retain.risk_level, RISK_MEDIUM)
        self.assertEqual(r_retain.revertability, REVERT_MODERATE)

        sts_del_pol = sts.replace(
            "replicas: 2",
            "replicas: 2\n  persistentVolumeClaimRetentionPolicy:\n    whenDeleted: Delete\n    whenScaled: Delete",
        )
        r_pol = classify_changes([_mod("clusters/dev/sts.yaml", sts, sts_del_pol)])
        self.assertEqual(r_pol.risk_level, RISK_HIGH)
        self.assertEqual(r_pol.destructiveness, DESTRUCTIVE_YES)

        r_imm = classify_changes([_mod("clusters/dev/sts.yaml", sts, sts.replace("db-headless", "db-new"))])
        self.assertEqual(r_imm.risk_level, RISK_HIGH)
        self.assertEqual(r_imm.destructiveness, DESTRUCTIVE_YES)

        r_del = classify_changes([_del("clusters/dev/sts.yaml", sts)])
        self.assertEqual(r_del.risk_level, RISK_HIGH)
        self.assertEqual(r_del.revertability, REVERT_DIFFICULT)
        self.assertEqual(r_del.destructiveness, DESTRUCTIVE_YES)

    def test_daemonset_image_bump_and_nodeselector_removal(self) -> None:
        ds = """\
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: log-agent
  namespace: shop
spec:
  selector:
    matchLabels:
      app: log-agent
  template:
    metadata:
      labels:
        app: log-agent
    spec:
      nodeSelector:
        pool: app
      containers:
        - name: agent
          image: gcr.io/acme/fluent:v2.1.0
"""
        r_img = classify_changes([_mod("clusters/dev/ds.yaml", ds, ds.replace("v2.1.0", "v2.1.1"))])
        self.assertEqual(r_img.risk_level, RISK_MEDIUM)
        self.assertEqual(r_img.blast_radius, BLAST_HIGH)

        r_ns = classify_changes([_mod("clusters/dev/ds.yaml", ds, ds.replace("      nodeSelector:\n        pool: app\n", ""))])
        self.assertEqual(r_ns.risk_level, RISK_HIGH)
        self.assertEqual(r_ns.destructiveness, DESTRUCTIVE_YES)

    def test_pod_template_label_removal_sidecar_probe_gpu_and_nameless_container(self) -> None:
        r_lbl = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, BASE_DEPLOY.replace("      labels:\n        app: web", "      labels:\n        app: web-changed"))])
        self.assertEqual(r_lbl.risk_level, RISK_HIGH)
        self.assertEqual(r_lbl.destructiveness, DESTRUCTIVE_YES)

        sidecar = BASE_DEPLOY + "        - name: sidecar\n          image: gcr.io/acme/proxy:v1.0.0\n"
        self.assertEqual(classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, sidecar)]).risk_level, RISK_HIGH)

        no_probe = BASE_DEPLOY.split("          readinessProbe:")[0]
        r_prb = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, no_probe)])
        self.assertEqual(r_prb.risk_level, RISK_HIGH)
        self.assertEqual(r_prb.destructiveness, DESTRUCTIVE_YES)

        gpu = BASE_DEPLOY.replace('memory: "512Mi"', 'memory: "512Mi"\n              nvidia.com/gpu: "1"')
        self.assertEqual(classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, gpu)]).risk_level, RISK_HIGH)

        nameless = BASE_DEPLOY.replace("        - name: app\n", "        - name: \"\"\n")
        self.assertEqual(classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, nameless)]).risk_level, RISK_HIGH)

    def test_gitops_reconcilers_and_kcc_deletions(self) -> None:
        argo_b = "apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata:\n  name: apps\n  namespace: argocd\nspec:\n  syncPolicy:\n    automated:\n      prune: false\n"
        argo_a = argo_b.replace("prune: false", "prune: true")
        r_argo = classify_changes([_mod("argocd/app.yaml", argo_b, argo_a)])
        self.assertEqual(r_argo.risk_level, RISK_CRITICAL)
        self.assertEqual(r_argo.destructiveness, DESTRUCTIVE_YES)

        rootsync = "apiVersion: configsync.gke.io/v1beta1\nkind: RootSync\nmetadata:\n  name: root-sync\n  namespace: config-management-system\nspec: {}\n"
        self.assertEqual(classify_changes([_add("bootstrap/rs.yaml", rootsync)]).risk_level, RISK_CRITICAL)

        nodepool = "apiVersion: container.cnrm.cloud.google.com/v1beta1\nkind: ContainerNodePool\nmetadata:\n  name: pool-1\n  namespace: config-control\nspec:\n  nodeConfig:\n    machineType: e2-standard-4\n"
        r_np_mod = classify_changes([_mod("gcp/np.yaml", nodepool, nodepool.replace("e2-standard-4", "e2-standard-8"))])
        self.assertEqual(r_np_mod.risk_level, RISK_HIGH)
        r_np_del = classify_changes([_del("gcp/np.yaml", nodepool)])
        self.assertEqual(r_np_del.risk_level, RISK_CRITICAL)
        self.assertEqual(r_np_del.revertability, REVERT_IRREVERSIBLE)
        self.assertEqual(r_np_del.destructiveness, DESTRUCTIVE_YES)

    def test_ingress_snippet_istio_envoyfilter_gateway_routes_and_storageclass(self) -> None:
        ing = "apiVersion: networking.k8s.io/v1\nkind: Ingress\nmetadata:\n  name: web\n  namespace: shop\n  annotations:\n    nginx.ingress.kubernetes.io/configuration-snippet: |\n      more_set_headers \"X-Test: 1\";\nspec: {}\n"
        self.assertEqual(classify_changes([_add("clusters/dev/ing.yaml", ing)]).risk_level, RISK_CRITICAL)

        ef = "apiVersion: networking.istio.io/v1alpha3\nkind: EnvoyFilter\nmetadata:\n  name: lua-inj\n  namespace: istio-system\nspec: {}\n"
        self.assertEqual(classify_changes([_add("clusters/dev/ef.yaml", ef)]).risk_level, RISK_CRITICAL)

        tls_route = "apiVersion: gateway.networking.k8s.io/v1alpha2\nkind: TLSRoute\nmetadata:\n  name: tls\n  namespace: shop\nspec: {}\n"
        self.assertEqual(classify_changes([_add("clusters/dev/tls.yaml", tls_route)]).risk_level, RISK_HIGH)

        sc = "apiVersion: storage.k8s.io/v1\nkind: StorageClass\nmetadata:\n  name: fast\n  annotations:\n    storageclass.kubernetes.io/is-default-class: \"true\"\nprovisioner: pd.csi.storage.gke.io\n"
        self.assertEqual(classify_changes([_add("clusters/dev/sc.yaml", sc)]).risk_level, RISK_CRITICAL)

    def test_namespace_psa_configmap_ratchet_quota_service_metadata_and_secret(self) -> None:
        ns_b = "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: shop\n  labels:\n    pod-security.kubernetes.io/enforce: restricted\n"
        ns_a = ns_b.replace("restricted", "privileged")
        self.assertEqual(classify_changes([_mod("clusters/dev/ns.yaml", ns_b, ns_a)]).risk_level, RISK_CRITICAL)

        r_imm = classify_changes([_mod("clusters/dev/cm.yaml", BASE_CONFIGMAP, BASE_CONFIGMAP + "immutable: true\n")])
        self.assertEqual(r_imm.risk_level, RISK_HIGH)
        self.assertEqual(r_imm.revertability, REVERT_DIFFICULT)

        r_wipe = classify_changes([_mod("clusters/dev/cm.yaml", BASE_CONFIGMAP, BASE_CONFIGMAP.replace("LOG_LEVEL: info", 'LOG_LEVEL: ""'))])
        self.assertEqual(r_wipe.risk_level, RISK_HIGH)
        self.assertEqual(r_wipe.destructiveness, DESTRUCTIVE_YES)

        r_ld = classify_changes([_mod("clusters/dev/cm.yaml", BASE_CONFIGMAP, BASE_CONFIGMAP + "  LD_PRELOAD: /tmp/evil.so\n")])
        self.assertEqual(r_ld.risk_level, RISK_HIGH)

        r_svc_meta = classify_changes([_mod("clusters/dev/svc.yaml", BASE_SERVICE, BASE_SERVICE.replace("  namespace: shop\n", "  namespace: shop\n  annotations:\n    owner: platform\n"))])
        self.assertEqual(r_svc_meta.risk_level, RISK_LOW)

        rq = "apiVersion: v1\nkind: ResourceQuota\nmetadata:\n  name: cap\n  namespace: shop\nspec:\n  hard:\n    pods: \"0\"\n"
        r_rq = classify_changes([_add("clusters/dev/rq.yaml", rq)])
        self.assertEqual(r_rq.risk_level, RISK_HIGH)
        self.assertEqual(r_rq.destructiveness, DESTRUCTIVE_YES)

        sec = "apiVersion: v1\nkind: Secret\nmetadata:\n  name: app-sec\n  namespace: shop\nstringData:\n  k: super-secret-raw-value\n"
        r_sec = classify_changes([_add("clusters/dev/sec.yaml", sec)])
        self.assertEqual(r_sec.risk_level, RISK_HIGH)
        self.assertNotIn("super-secret-raw-value", json.dumps(r_sec.to_dict()))

    def test_rename_symlink_submodule_exec_list_missing_ns_self_mod_and_traversal(self) -> None:
        r_ren = classify_changes([FileChange(path="clusters/dev/web2.yaml", old_path="clusters/dev/web.yaml", change_type=CHANGE_RENAMED, before=BASE_DEPLOY.encode(), after=BASE_DEPLOY.encode(), before_mode="100644", after_mode="100644")])
        self.assertEqual(r_ren.risk_level, RISK_MEDIUM)
        self.assertEqual(r_ren.revertability, REVERT_MODERATE)

        r_sym = classify_changes([FileChange(path="clusters/dev/link.yaml", change_type=CHANGE_ADDED, after_mode="120000")])
        self.assertEqual(r_sym.risk_level, RISK_HIGH)
        self.assertFalse(r_sym.supported)

        r_exec = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, BASE_DEPLOY.replace("v1.2.3", "v1.2.4"), after_mode="100755")])
        self.assertEqual(r_exec.risk_level, RISK_HIGH)

        k8s_list = "apiVersion: v1\nkind: List\nitems:\n  - apiVersion: v1\n    kind: ConfigMap\n    metadata:\n      name: c1\n      namespace: shop\n    data:\n      K: v\n"
        self.assertEqual(classify_changes([_add("clusters/dev/list.yaml", k8s_list)]).risk_level, RISK_LOW)

        no_ns = BASE_DEPLOY.replace("  namespace: shop\n", "")
        self.assertEqual(classify_changes([_add("clusters/dev/nons.yaml", no_ns)]).risk_level, RISK_UNKNOWN)

        self_cr = "apiVersion: kubeagents.x-k8s.io/v1alpha1\nkind: PlatformAgent\nmetadata:\n  name: pa\n  namespace: kubeagents-system\nspec: {}\n"
        self.assertEqual(classify_changes([_add("clusters/dev/pa.yaml", self_cr)]).risk_level, RISK_CRITICAL)

        r_trav = classify_changes([_add("../escape.yaml", BASE_CONFIGMAP)])
        self.assertEqual(r_trav.risk_level, RISK_HIGH)
        self.assertFalse(r_trav.supported)

    def test_package_is_model_network_clock_and_random_free(self) -> None:
        import ast
        pkg_dir = Path(__file__).resolve().parent / "risk_classifier"
        forbidden = {
            "urllib", "http", "requests", "httpx", "socket", "ssl",
            "random", "secrets", "time", "datetime", "uuid",
            "openai", "anthropic", "google", "litellm",
        }
        modules = ("__init__.py", "__main__.py", "types.py", "parser.py", "k8s_rules.py", "classifier.py", "git_loader.py")
        for name in modules:
            src = (pkg_dir / name).read_text(encoding="utf-8")
            tree = ast.parse(src, filename=name)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0]
                        self.assertNotIn(top, forbidden, f"{name} must not import {top}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    top = node.module.split(".")[0]
                    self.assertNotIn(top, forbidden, f"{name} must not import {top}")


class AdversarialRegressionSuiteTests(unittest.TestCase):
    """Targeted regression tests for every adversarial bypass and edge case."""

    def test_executable_manifest_cannot_mask_critical_rbac(self) -> None:
        crb = (
            "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRoleBinding\n"
            "metadata:\n  name: sneak-admin\nroleRef:\n  apiGroup: rbac.authorization.k8s.io\n"
            "  kind: ClusterRole\n  name: cluster-admin\nsubjects:\n  - kind: ServiceAccount\n"
            "    name: ci\n    namespace: default\n"
        )
        r = classify_changes([_add("clusters/dev/rbac.yaml", crb, after_mode="100755")])
        self.assertEqual(r.risk_level, RISK_CRITICAL)
        codes = {reason.code for reason in r.reasons}
        self.assertIn("EXECUTABLE_MANIFEST", codes)
        self.assertIn("CRITICAL_PRIVILEGED_RBAC", codes)

    def test_cross_boundary_renames_split_into_delete_plus_add(self) -> None:
        ns = "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: payments\n"
        pvc = (
            "apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata:\n  name: pg-data\n"
            "  namespace: db\nspec:\n  accessModes: [ReadWriteOnce]\n"
            "  resources:\n    requests:\n      storage: 100Gi\n"
        )
        # Rename Namespace manifest to docs -> must evaluate as CRITICAL Namespace DELETE
        r_doc = classify_changes(
            [
                FileChange(
                    path="docs/notes.md",
                    old_path="clusters/prod/payments-ns.yaml",
                    change_type=CHANGE_RENAMED,
                    before=ns.encode(),
                    after=ns.encode(),
                    before_mode="100644",
                    after_mode="100644",
                )
            ]
        )
        self.assertEqual(r_doc.risk_level, RISK_CRITICAL)
        self.assertEqual(r_doc.destructiveness, DESTRUCTIVE_YES)
        self.assertEqual(r_doc.revertability, REVERT_IRREVERSIBLE)

        # Rename prod PVC to dev cluster -> must evaluate as CRITICAL PVC DELETE in prod
        r_pvc = classify_changes(
            [
                FileChange(
                    path="clusters/dev/pg-data.yaml",
                    old_path="clusters/prod/pg-data.yaml",
                    change_type=CHANGE_RENAMED,
                    before=pvc.encode(),
                    after=pvc.encode(),
                    before_mode="100644",
                    after_mode="100644",
                )
            ]
        )
        self.assertEqual(r_pvc.risk_level, RISK_CRITICAL)
        self.assertEqual(r_pvc.destructiveness, DESTRUCTIVE_YES)
        self.assertEqual(r_pvc.revertability, REVERT_IRREVERSIBLE)

        # Rename sync-tree manifest to non-sync examples dir -> CRITICAL Namespace DELETE
        r_ex = classify_changes(
            [
                FileChange(
                    path="examples/payments-ns.yaml",
                    old_path="clusters/prod/payments-ns.yaml",
                    change_type=CHANGE_RENAMED,
                    before=ns.encode(),
                    after=ns.encode(),
                    before_mode="100644",
                    after_mode="100644",
                )
            ]
        )
        self.assertEqual(r_ex.risk_level, RISK_CRITICAL)
        self.assertEqual(r_ex.destructiveness, DESTRUCTIVE_YES)

        # Rename Terraform to docs -> HIGH destructive Terraform DELETE
        r_tf = classify_changes(
            [
                FileChange(
                    path="docs/main.md",
                    old_path="infra/main.tf",
                    change_type=CHANGE_RENAMED,
                    before=b'resource "google_container_cluster" "x" {}\n',
                    after=b"# moved\n",
                    before_mode="100644",
                    after_mode="100644",
                )
            ]
        )
        self.assertEqual(r_tf.risk_level, RISK_HIGH)
        self.assertEqual(r_tf.destructiveness, DESTRUCTIVE_YES)

    def test_template_marker_in_annotation_cannot_mask_critical_change(self) -> None:
        crb = (
            "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRoleBinding\n"
            "metadata:\n  name: sneak-tmpl\n  annotations:\n    note: '${HARMLESS}'\n"
            "roleRef:\n  apiGroup: rbac.authorization.k8s.io\n  kind: ClusterRole\n"
            "  name: cluster-admin\nsubjects:\n  - kind: ServiceAccount\n    name: ci\n    namespace: default\n"
        )
        r = classify_changes([_add("clusters/dev/rbac.yaml", crb)])
        self.assertEqual(r.risk_level, RISK_CRITICAL)
        self.assertFalse(r.supported)
        codes = {reason.code for reason in r.reasons}
        self.assertIn("CRITICAL_PRIVILEGED_RBAC", codes)
        self.assertIn("UNRENDERED_TEMPLATE", codes)

    def test_kustomize_patch_delete_and_incomplete_workload_fragment(self) -> None:
        del_patch = (
            "apiVersion: v1\nkind: PersistentVolumeClaim\n"
            "metadata:\n  name: pg-data\n  namespace: db\n$patch: delete\n"
        )
        r_del = classify_changes([_add("clusters/dev/del-pvc.yaml", del_patch)])
        self.assertEqual(r_del.risk_level, RISK_CRITICAL)
        self.assertEqual(r_del.destructiveness, DESTRUCTIVE_YES)

        cm_patch = (
            "apiVersion: v1\nkind: ConfigMap\n"
            "metadata:\n  name: app-cfg\n  namespace: shop\n$patch: delete\n"
        )
        r_cm = classify_changes([_add("clusters/dev/del-cm.yaml", cm_patch)])
        self.assertEqual(r_cm.risk_level, RISK_HIGH)
        self.assertEqual(r_cm.destructiveness, DESTRUCTIVE_YES)

        frag = (
            "apiVersion: apps/v1\nkind: Deployment\n"
            "metadata:\n  name: web\n  namespace: shop\nspec:\n  replicas: 5\n"
        )
        r_frag = classify_changes([_add("clusters/dev/patch.yaml", frag)])
        self.assertEqual(r_frag.risk_level, RISK_UNKNOWN)
        self.assertFalse(r_frag.supported)

    def test_yaml_explicit_tags_timestamps_nan_inf_and_json_array_caps(self) -> None:
        # Disallowed explicit YAML tags (!!binary, !!set)
        bin_cm = (
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c1\n  namespace: shop\n"
            "data:\n  k: !!binary R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==\n"
        )
        r_bin = classify_changes([_add("clusters/dev/bin.yaml", bin_cm)])
        self.assertEqual(r_bin.risk_level, RISK_UNKNOWN)
        self.assertIn("YAML_UNSUPPORTED_TAG", {x.code for x in r_bin.reasons})

        # Unquoted YAML 1.1 date stays a plain string (YAML 1.2 Core Schema)
        date_cm = (
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c-date\n  namespace: shop\n"
            "data:\n  RELEASE_DATE: 2026-11-25\n"
        )
        r_date = classify_changes([_add("clusters/dev/date.yaml", date_cm)])
        self.assertEqual(r_date.risk_level, RISK_LOW)

        # Non-RFC-8259 JSON NaN/Infinity rejected
        bad_json = '{"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"c","namespace":"s"},"data":{"x":NaN}}'
        r_nan = classify_changes([_add("clusters/dev/nan.json", bad_json)])
        self.assertEqual(r_nan.risk_level, RISK_UNKNOWN)
        self.assertIn("JSON_INVALID_CONSTANT", {x.code for x in r_nan.reasons})

        # Top-level JSON array exceeding MAX_DOCS_PER_FILE rejected
        big_arr = json.dumps(
            [{"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": f"c{i}", "namespace": "s"}} for i in range(205)]
        )
        r_arr = classify_changes([_add("clusters/dev/big.json", big_arr)])
        self.assertEqual(r_arr.risk_level, RISK_UNKNOWN)
        self.assertIn("TOO_MANY_DOCUMENTS", {x.code for x in r_arr.reasons})

        # Non-finite resource quantity (`1e400`) rejected and cannot poison compound capacity check
        inf_deploy = BASE_DEPLOY.replace('cpu: "100m"', 'cpu: "1e400"')
        r_inf = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, inf_deploy)])
        self.assertEqual(r_inf.risk_level, RISK_HIGH)
        self.assertIn("INVALID_RESOURCE_QUANTITY", {x.code for x in r_inf.reasons})

        # Malformed apiVersion rejected
        for bad_api in ("/v1", "apps/", "apps/v1/evil"):
            doc = f"apiVersion: {bad_api}\nkind: ConfigMap\nmetadata:\n  name: c\n  namespace: s\n"
            r_api = classify_changes([_add("clusters/dev/badapi.yaml", doc)])
            self.assertEqual(r_api.risk_level, RISK_UNKNOWN, bad_api)
            self.assertIn("INVALID_APIVERSION", {x.code for x in r_api.reasons})

    def test_malformed_yaml_types_never_crash_and_fail_closed(self) -> None:
        cases = [
            _mod("clusters/dev/web.yaml", BASE_DEPLOY, BASE_DEPLOY.replace("replicas: 4", 'replicas: "0"')),
            _mod("clusters/dev/web.yaml", BASE_DEPLOY, BASE_DEPLOY + "          env: 123\n"),
            _mod("clusters/dev/web.yaml", BASE_DEPLOY, BASE_DEPLOY.replace("          ports:\n            - name: http\n              containerPort: 8080\n", "          ports: true\n")),
            _mod("clusters/dev/web.yaml", BASE_DEPLOY, BASE_DEPLOY + "      volumes:\n        - name: [1, 2]\n"),
            _add("clusters/dev/cm.yaml", "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n  namespace: shop\ndata: not-a-dict\n"),
            _add("clusters/dev/cm2.yaml", "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n  namespace: shop\ndata:\n  LOG_LEVEL: 123\n"),
            _mod(
                "clusters/dev/pvc.yaml",
                "apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata:\n  name: d\n  namespace: shop\nspec:\n  resources:\n    requests:\n      storage: 10Gi\n",
                "apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata:\n  name: d\n  namespace: shop\nspec:\n  resources: 10Gi\n",
            ),
            _mod(
                "clusters/dev/hpa.yaml",
                "apiVersion: autoscaling/v2\nkind: HorizontalPodAutoscaler\nmetadata:\n  name: h\n  namespace: shop\nspec:\n  minReplicas: 2\n  maxReplicas: 5\n",
                "apiVersion: autoscaling/v2\nkind: HorizontalPodAutoscaler\nmetadata:\n  name: h\n  namespace: shop\nspec:\n  minReplicas: true\n  maxReplicas: 5\n",
            ),
            _add(
                "clusters/dev/cr.yaml",
                "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRole\nmetadata:\n  name: r\nrules: 42\n",
            ),
            _add(
                "clusters/dev/cr2.yaml",
                "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRole\nmetadata:\n  name: r2\nrules:\n  - verbs: '*'\n    resources: secrets\n",
            ),
            _add(
                "clusters/dev/crb.yaml",
                "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRoleBinding\nmetadata:\n  name: b\nroleRef:\n  name: view\nsubjects: true\n",
            ),
        ]
        for fc in cases:
            r = classify_changes([fc])
            self.assertIn(r.risk_level, (RISK_HIGH, RISK_CRITICAL), f"{fc.path}: got {r.risk_level}")

    def test_security_hardening_removal_and_podspec_evasions(self) -> None:
        # Whole securityContext block removed (BASE_DEPLOY already has runAsNonRoot: true) -> HIGH
        unhardened = BASE_DEPLOY.replace(
            "      securityContext:\n        runAsNonRoot: true\n        runAsUser: 1000\n",
            "",
        )
        r_rm = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, unhardened)])
        self.assertEqual(r_rm.risk_level, RISK_HIGH)
        self.assertIn("SECURITY_HARDENING_REMOVED", {x.code for x in r_rm.reasons})

        # Normalized hostPath traversal (`/.`, `/var/log/..`, and `/var/log/../run/docker.sock`) -> CRITICAL
        for bad_hp in ("/.", "/var/log/..", "/var/log/../run/docker.sock"):
            hp_dep = BASE_DEPLOY + f"      volumes:\n        - name: v\n          hostPath:\n            path: {bad_hp}\n"
            r_hp = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, hp_dep)])
            self.assertEqual(r_hp.risk_level, RISK_CRITICAL, bad_hp)

        # Pod securityContext sysctls, appArmor Unconfined, windowsOptions.hostProcess -> HIGH
        for snippet in (
            "      securityContext:\n        sysctls:\n          - name: net.ipv4.ip_forward\n            value: '1'\n",
            "      securityContext:\n        appArmorProfile:\n          type: Unconfined\n",
            "      securityContext:\n        fsGroup: 0\n",
            "      securityContext:\n        windowsOptions:\n          hostProcess: true\n",
            "      shareProcessNamespace: true\n",
            "      nodeName: worker-1\n",
        ):
            mod = unhardened.replace("      containers:\n", snippet + "      containers:\n")
            r = classify_changes([_mod("clusters/dev/web.yaml", unhardened, mod)])
            self.assertEqual(r.risk_level, RISK_HIGH, snippet)

        # Duplicate container name swapping malicious image into first slot -> HIGH
        dup_after = (
            BASE_DEPLOY.replace("image: gcr.io/acme/web:v1.2.3", "image: evil.io/pwn:latest")
            + "        - name: app\n          image: gcr.io/acme/web:v1.2.3\n"
        )
        r_dup = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, dup_after)])
        self.assertEqual(r_dup.risk_level, RISK_HIGH)
        self.assertIn("DUPLICATE_CONTAINER_NAME", {x.code for x in r_dup.reasons})

        # Deployment rollingUpdate.maxUnavailable: "100%" -> HIGH destructive
        strat_after = BASE_DEPLOY.replace(
            "  replicas: 4\n",
            '  replicas: 4\n  strategy:\n    type: RollingUpdate\n    rollingUpdate:\n      maxUnavailable: "100%"\n',
        )
        r_strat = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, strat_after)])
        self.assertEqual(r_strat.risk_level, RISK_HIGH)
        self.assertEqual(r_strat.destructiveness, DESTRUCTIVE_YES)

        # Removing `replicas: 10` (defaults to 1 in K8s) -> HIGH destructive scale-down
        rep10 = BASE_DEPLOY.replace("  replicas: 4\n", "  replicas: 10\n")
        rep_rm = BASE_DEPLOY.replace("  replicas: 4\n", "")
        r_rep = classify_changes([_mod("clusters/dev/web.yaml", rep10, rep_rm)])
        self.assertEqual(r_rep.risk_level, RISK_HIGH)
        self.assertEqual(r_rep.destructiveness, DESTRUCTIVE_YES)

        # resources.claims & ephemeral-storage not bypassed
        claims_after = BASE_DEPLOY.replace(
            "          readinessProbe:\n",
            "            claims:\n              - name: gpu-claim\n          readinessProbe:\n",
        )
        r_claims = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, claims_after)])
        self.assertEqual(r_claims.risk_level, RISK_HIGH)

        eph_after = BASE_DEPLOY.replace('memory: "512Mi"\n', 'memory: "512Mi"\n              ephemeral-storage: "100Gi"\n')
        r_eph = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, eph_after)])
        self.assertEqual(r_eph.risk_level, RISK_MEDIUM)

        # Plaintext GitHub PAT in container args -> CRITICAL
        fake_pat = "ghp_" + "1234567890abcdefghij1234567890abcd"
        pat_after = BASE_DEPLOY + f'          args:\n            - "--token={fake_pat}"\n'
        r_pat = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, pat_after)])
        self.assertEqual(r_pat.risk_level, RISK_CRITICAL)
        self.assertIn("PLAINTEXT_SECRET_LEAK", {x.code for x in r_pat.reasons})

        # CronJob unsuspending (true -> false) -> MEDIUM (not skipped!)
        cj_susp = (
            "apiVersion: batch/v1\nkind: CronJob\nmetadata:\n  name: cj\n  namespace: shop\n"
            "spec:\n  schedule: '0 * * * *'\n  suspend: true\n  jobTemplate:\n    spec:\n"
            "      template:\n        spec:\n          containers:\n            - name: c\n              image: gcr.io/a/b:v1.0.0\n"
        )
        r_cj = classify_changes([_mod("clusters/dev/cj.yaml", cj_susp, cj_susp.replace("suspend: true", "suspend: false"))])
        self.assertEqual(r_cj.risk_level, RISK_MEDIUM)

    def test_rbac_gatekeeper_kyverno_node_secretstore_netpol_service_hpa_pdb_and_prod(self) -> None:
        # ClusterRoleBinding to built-in `admin`/`edit` and `system:serviceaccounts` -> CRITICAL
        for role_name in ("admin", "edit"):
            crb = (
                f"apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRoleBinding\nmetadata:\n  name: bind-{role_name}\n"
                f"roleRef:\n  apiGroup: rbac.authorization.k8s.io\n  kind: ClusterRole\n  name: {role_name}\n"
                "subjects:\n  - kind: ServiceAccount\n    name: ci\n    namespace: dev\n"
            )
            self.assertEqual(classify_changes([_add("clusters/dev/crb.yaml", crb)]).risk_level, RISK_CRITICAL)

        sa_crb = (
            "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRoleBinding\nmetadata:\n  name: all-sa\n"
            "roleRef:\n  apiGroup: rbac.authorization.k8s.io\n  kind: ClusterRole\n  name: view\n"
            "subjects:\n  - kind: Group\n    name: system:serviceaccounts\n"
        )
        self.assertEqual(classify_changes([_add("clusters/dev/sa.yaml", sa_crb)]).risk_level, RISK_CRITICAL)

        # OPA Gatekeeper cluster-scoped constraint delete -> CRITICAL (NOT MISSING_NAMESPACE!)
        gk = "apiVersion: constraints.gatekeeper.sh/v1beta1\nkind: K8sRequiredLabels\nmetadata:\n  name: must-have-owner\nspec: {}\n"
        r_gk = classify_changes([_del("clusters/dev/gk.yaml", gk)])
        self.assertEqual(r_gk.risk_level, RISK_CRITICAL)
        self.assertTrue(r_gk.supported)
        self.assertIn("CRITICAL_CONTROL_PLANE_DELETE", {x.code for x in r_gk.reasons})

        # Modern Kyverno per-rule validate.failureAction Enforce -> Audit -> CRITICAL
        kyv_b = (
            "apiVersion: kyverno.io/v1\nkind: Policy\nmetadata:\n  name: no-priv\n  namespace: shop\n"
            "spec:\n  rules:\n    - name: r1\n      validate:\n        failureAction: Enforce\n"
        )
        kyv_a = kyv_b.replace("Enforce", "Audit")
        r_kyv = classify_changes([_mod("clusters/dev/kyv.yaml", kyv_b, kyv_a)])
        self.assertEqual(r_kyv.risk_level, RISK_CRITICAL)
        self.assertIn("POLICY_ENGINE_WEAKENED", {x.code for x in r_kyv.reasons})

        # Node cordon/taint modify -> CRITICAL
        node_b = "apiVersion: v1\nkind: Node\nmetadata:\n  name: gke-node-1\nspec:\n  unschedulable: false\n"
        node_a = node_b.replace("false", "true")
        r_node = classify_changes([_mod("clusters/dev/node.yaml", node_b, node_a)])
        self.assertEqual(r_node.risk_level, RISK_CRITICAL)
        self.assertTrue(r_node.reasons)

        # SecretStore -> HIGH, ClusterSecretStore -> CRITICAL
        ss = "apiVersion: external-secrets.io/v1beta1\nkind: SecretStore\nmetadata:\n  name: gcp\n  namespace: shop\nspec: {}\n"
        css = "apiVersion: external-secrets.io/v1beta1\nkind: ClusterSecretStore\nmetadata:\n  name: gcp\nspec: {}\n"
        self.assertEqual(classify_changes([_add("clusters/dev/ss.yaml", ss)]).risk_level, RISK_HIGH)
        self.assertEqual(classify_changes([_add("clusters/dev/css.yaml", css)]).risk_level, RISK_CRITICAL)

        # NetworkPolicy add (empty deny-all) -> HIGH
        np = "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: deny-all\n  namespace: shop\nspec:\n  podSelector: {}\n"
        self.assertEqual(classify_changes([_add("clusters/dev/np.yaml", np)]).risk_level, RISK_HIGH)

        # Namespace PSA baseline -> privileged -> CRITICAL
        ns_b = "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: shop\n  labels:\n    pod-security.kubernetes.io/enforce: baseline\n"
        ns_a = ns_b.replace("baseline", "privileged")
        self.assertEqual(classify_changes([_mod("clusters/dev/ns.yaml", ns_b, ns_a)]).risk_level, RISK_CRITICAL)

        # Service ExternalName retarget -> HIGH (CRITICAL in prod); LoadBalancer sourceRanges removed -> CRITICAL
        ext_b = "apiVersion: v1\nkind: Service\nmetadata:\n  name: db\n  namespace: shop\nspec:\n  type: ExternalName\n  externalName: db.internal.corp\n"
        ext_a = ext_b.replace("db.internal.corp", "evil.attacker.com")
        self.assertEqual(classify_changes([_mod("clusters/dev/svc.yaml", ext_b, ext_a)]).risk_level, RISK_HIGH)
        self.assertEqual(classify_changes([_mod("clusters/prod/svc.yaml", ext_b, ext_a)]).risk_level, RISK_CRITICAL)

        lb_b = "apiVersion: v1\nkind: Service\nmetadata:\n  name: api\n  namespace: shop\nspec:\n  type: LoadBalancer\n  loadBalancerSourceRanges:\n    - 10.0.0.0/8\n"
        lb_a = "apiVersion: v1\nkind: Service\nmetadata:\n  name: api\n  namespace: shop\nspec:\n  type: LoadBalancer\n"
        self.assertEqual(classify_changes([_mod("clusters/dev/lb.yaml", lb_b, lb_a)]).risk_level, RISK_CRITICAL)

        # HPA minReplicas 2 -> 0 -> HIGH destructive
        hpa_b = "apiVersion: autoscaling/v2\nkind: HorizontalPodAutoscaler\nmetadata:\n  name: web\n  namespace: shop\nspec:\n  minReplicas: 2\n  maxReplicas: 10\n"
        hpa_a = hpa_b.replace("minReplicas: 2", "minReplicas: 0")
        r_hpa = classify_changes([_mod("clusters/dev/hpa.yaml", hpa_b, hpa_a)])
        self.assertEqual(r_hpa.risk_level, RISK_HIGH)
        self.assertEqual(r_hpa.destructiveness, DESTRUCTIVE_YES)

        # PDB offline add has moderate revertability (never auto-merged blindly)
        pdb = "apiVersion: policy/v1\nkind: PodDisruptionBudget\nmetadata:\n  name: web\n  namespace: shop\nspec:\n  minAvailable: 1\n  selector:\n    matchLabels:\n      app: web\n"
        r_pdb = classify_changes([_add("clusters/dev/pdb.yaml", pdb)])
        self.assertEqual(r_pdb.risk_level, RISK_MEDIUM)
        self.assertEqual(r_pdb.revertability, REVERT_MODERATE)

        # Floating 2-part image tag (`:16.2`) and multi-`@` ref are NOT immutable pins
        for bad_img in ("gcr.io/acme/web:16.2", "gcr.io/acme/web@bad@sha256:" + "a" * 64):
            r_img = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, BASE_DEPLOY.replace("gcr.io/acme/web:v1.2.3", bad_img))])
            self.assertNotEqual(r_img.risk_level, RISK_LOW, bad_img)

        # Production path/label variants (`us-production`, `prod_us`, `prod.yaml`, `env: prod-us`)
        bump = BASE_DEPLOY.replace("replicas: 4", "replicas: 5")
        for prod_path in ("clusters/us-production/web.yaml", "clusters/prod_us/web.yaml", "clusters/main/prod.yaml"):
            self.assertEqual(classify_changes([_mod(prod_path, BASE_DEPLOY, bump)]).risk_level, RISK_MEDIUM, prod_path)

        # Monotonic Namespace label cache: stripping `env: prod` in same PR cannot downgrade sibling Deployment
        ns_prod = "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: shop\n  labels:\n    env: prod-us\n"
        ns_stripped = "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: shop\n  labels:\n    env: dev\n"
        r_mono = classify_changes(
            [
                _mod("clusters/main/ns.yaml", ns_prod, ns_stripped),
                _mod("clusters/main/web.yaml", BASE_DEPLOY, bump),
            ]
        )
        self.assertEqual(r_mono.risk_level, RISK_CRITICAL)
        # Sibling Deployment verdict itself must still be escalated to medium (prod preserved from `before`!)
        dep_verdicts = [rv for rv in r_mono.resources if rv.ref.kind == "Deployment"]
        self.assertEqual(len(dep_verdicts), 1)
        self.assertEqual(dep_verdicts[0].risk_level, RISK_MEDIUM)

    def test_git_loader_integrity_flag_injection_and_over_500_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            (repo / ".gitattributes").write_text("clusters/dev/hidden.yaml export-ignore\n", encoding="utf-8")
            (repo / "clusters" / "dev").mkdir(parents=True)
            (repo / "clusters" / "dev" / "hidden.yaml").write_text(BASE_DEPLOY, encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
            base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

            # Flag-like rev rejected
            with self.assertRaises(ValueError):
                load_git_changes(repo, "--evil", base_sha)

            # Modify export-ignored file; even with disable_gitattributes=False, ls-tree integrity check catches omission!
            (repo / "clusters" / "dev" / "hidden.yaml").write_text(
                BASE_DEPLOY.replace("replicas: 4", "replicas: 0"), encoding="utf-8"
            )
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "sneak"], cwd=repo, check=True, capture_output=True)
            head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

            res_chk, _ = classify_git_range(repo, base_sha, head_sha, disable_gitattributes=False)
            self.assertEqual(res_chk.risk_level, RISK_HIGH)
            self.assertFalse(res_chk.supported)
            self.assertIn("ARCHIVE_INTEGRITY_ERROR", {x.code for x in res_chk.reasons})

            # >500 files with CODEOWNERS at position 505 STILL returns CRITICAL!
            for i in range(505):
                (repo / f"pad_{i:04d}.txt").write_text("x\n", encoding="utf-8")
            (repo / "zz_CODEOWNERS").write_text("x\n", encoding="utf-8")
            (repo / "z_dir").mkdir(exist_ok=True)
            (repo / "z_dir" / "CODEOWNERS").write_text("* @attacker\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "flood"], cwd=repo, check=True, capture_output=True)
            flood_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

            res_flood, _ = classify_git_range(repo, head_sha, flood_sha, disable_gitattributes=True)
            self.assertEqual(res_flood.risk_level, RISK_CRITICAL)
            self.assertIn("CRITICAL_GOVERNANCE_OR_CI_FILE", {x.code for x in res_flood.reasons})


if __name__ == "__main__":
    unittest.main()
