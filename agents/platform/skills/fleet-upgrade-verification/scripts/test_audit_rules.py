#!/usr/bin/env python3
"""Unit tests for the readiness rules of catalogue entries 6, 8, 9 and 10 on fixture JSON.

The audit-log rules run over `testdata/audit_log_sample.json` through the fake runner
`test_audit_log.py` defines (the sample's provenance is in that file's docstring); the
report-level tests run `fleet_upgrade_report.main` with `test_fleet_upgrade_report.py`'s
fakes, so the four rules are exercised through the registry, the fold and the table. No
test runs gcloud or kubectl.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import fleet_upgrade_report as report  # noqa: E402
import readiness_rules as shared  # noqa: E402
import upgrade_readiness as readiness  # noqa: E402
import upgrade_shape_tables as tables  # noqa: E402
from readiness_rules import audit_log, changed_defaults, client_addon_skew, deprecated_api_callers, removed_api_callers  # noqa: E402
from test_audit_log import AT, ENDPOINT_CONTROLLER, FAIL, STALE_KUBECTL, TUNER, WRITER, FakeRunner, context_with, full_page, sample_context, sample_entries, split_sample  # noqa: E402
from test_fleet_upgrade_report import FakeReadinessCommands, cluster, k8s_pdb, k8s_workload  # noqa: E402

GATEWAY_OPERATOR = "system:serviceaccount:mesh:gateway-operator"
TARGET_1_35 = (1, 35, 1, 1000)
TARGET_1_31 = (1, 31, 9, 1)
CLEAR = {"blocking_exclusions": [], "undecided_exclusions": []}
NO_SKEW = {"blocking": [], "unknown": []}
NO_WEBHOOKS = {"blocking": []}
RULES_C = (removed_api_callers, changed_defaults, deprecated_api_callers, client_addon_skew)


def read(items=None, error=None, read_errors=None):
    """A `read` dict as fleet_upgrade_report.read_cluster_objects returns it."""
    errors = {kind: None for kind in shared.READ_KINDS}
    errors.update(read_errors or {})
    return {"kubeconfig": "k", "items": items, "error": error, "read_errors": errors}


def member(master="1.33.5-gke.100", pools=("1.33.5-gke.100",)):
    return {
        "project": "example-project",
        "cluster": "upg-06",
        "location": "us-central1-a",
        "control_plane_version": master,
        "node_pools": [{"name": f"pool-{i}", "version": v, "status": "RUNNING"} for i, v in enumerate(pools)],
        "target_version": None,
    }


def namespace(name, **labels):
    return {"kind": "Namespace", "metadata": {"name": name, "labels": labels}}


def psa_namespace(name, enforce, version=None):
    labels = {"pod-security.kubernetes.io/enforce": enforce}
    if version is not None:
        labels["pod-security.kubernetes.io/enforce-version"] = version
    return namespace(name, **labels)


def workload(kind, ns, name, image, volumes=None):
    pod_spec = {"containers": [{"name": "c", "image": image}]}
    if volumes:
        pod_spec["volumes"] = volumes
    template = {"metadata": {"labels": {"app": name}}, "spec": pod_spec}
    if kind == "CronJob":
        return {"kind": kind, "metadata": {"namespace": ns, "name": name}, "spec": {"schedule": "*/10 * * * *", "jobTemplate": {"spec": {"template": template}}}}
    return {"kind": kind, "metadata": {"namespace": ns, "name": name}, "spec": {"replicas": 1, "template": template}}


def with_principal(entry, principal):
    """A copy of `entry` from `principal`, with an insertId of its own: the read drops an
    entry whose id it has already seen, so two callers built from one sample entry must
    not share one."""
    copy = json.loads(json.dumps(entry))
    copy["protoPayload"]["authenticationInfo"]["principalEmail"] = principal
    copy[audit_log.INSERT_ID] = f"{copy.get(audit_log.INSERT_ID, 'entry')}-{principal}"
    return copy


def status_with(rule, result):
    """The member verdict with only `rule`'s result folded in, every other rule clean."""
    folded = readiness.evaluate_extra_rules({}, member(), read([]), TARGET_1_35, context_with(), rules=[])
    folded = {**folded, **{key: list(result.get(key) or []) for key in ("blocking", "risks", "unknown")}}
    folded["unknown"] = [{"reason": u["reason"], "can_block": rule.CAN_BLOCK} for u in folded["unknown"]]
    return readiness.readiness_status({"blocking": []}, NO_WEBHOOKS, CLEAR, NO_SKEW, True, folded)


class ContractTest(unittest.TestCase):
    def test_the_four_rules_meet_the_contract_and_are_registered_by_entry(self):
        self.assertEqual([(m.RULE_ID, m.ENTRY, m.CAN_BLOCK) for m in RULES_C], [("removed-api-callers", 6, True), ("changed-defaults", 8, False), ("deprecated-api-callers", 9, False), ("client-addon-skew", 10, False)])
        for module in RULES_C:
            self.assertIn(module, readiness.EXTRA_RULES, module.RULE_ID)
            self.assertTrue(callable(module.evaluate) and callable(module.describe))
        entries = [m.ENTRY for m in readiness.EXTRA_RULES if m.ENTRY is not None]  # the plan-level rules, with no entry, come after the numbered ones
        self.assertEqual(entries, sorted(entries))

    def test_every_rule_runs_over_two_shared_reads_through_the_fold(self):
        context = sample_context()
        items = ClientAddonSkewTest.ITEMS + [psa_namespace("seeded-defaults", "baseline", "latest")]
        folded = readiness.evaluate_extra_rules(ClientAddonSkewTest.CLUSTER, member("1.31.5-gke.100", ("1.31.5-gke.100",)), read(items), TARGET_1_35, context, rules=list(RULES_C))
        self.assertEqual(len(context[shared.CONTEXT_RUN_CMD].calls), 2)
        self.assertEqual([f["rule"] for f in folded["blocking"]], ["removed-api-callers"])
        self.assertIn(TUNER, folded["blocking"][0]["text"])
        self.assertEqual({f["rule"] for f in folded["risks"]}, {"changed-defaults", "deprecated-api-callers", "client-addon-skew"})
        self.assertTrue(all("text" in f for f in folded["risks"]))
        self.assertEqual(folded["unknown"], [])
        self.assertEqual(readiness.readiness_status({"blocking": []}, NO_WEBHOOKS, CLEAR, NO_SKEW, True, folded), "blocked")
        self.assertEqual(folded["rules"]["removed-api-callers"], {"entry": 6, "can_block": True, "blocking": 1, "risks": 0, "unknown": 0, "notes": 1})


class RemovedApiCallersTest(unittest.TestCase):
    # The sample's removal is 1.32: this upgrade's block on a 1.31 control plane, a crossing on
    # the helper's default 1.33.
    BEHIND = dict(master="1.31.5-gke.100", pools=("1.31.5-gke.100",))

    def test_a_recent_operator_caller_of_a_removed_api_blocks(self):
        result = removed_api_callers.evaluate({}, member(**self.BEHIND), read([]), TARGET_1_35, sample_context())
        self.assertEqual(len(result["blocking"]), 1)
        item = result["blocking"][0]
        self.assertEqual((item["principal"], item["caller_class"], item["removed_release"], item["replacement"]), (TUNER, audit_log.CLASS_OPERATOR, "1.32", "flowcontrol.apiserver.k8s.io/v1"))
        self.assertEqual(item["api"], "flowcontrol.apiserver.k8s.io/v1beta3 flowschemas")
        self.assertEqual((result["risks"], result["unknown"]), ([], []))
        text = removed_api_callers.describe(item)
        self.assertIn(f"{TUNER} via legacy-flowcontrol-tuner/0.3 writes flowcontrol.apiserver.k8s.io/v1beta3 flowschemas, removed in 1.32 (2 writes in 7d, last 2026-09-29T14:04:19.112Z)", text)
        self.assertIn("removed at or before the target 1.35, last write 2026-09-29T14:04:19.112Z; this caller fails", text)
        self.assertIn("replacement flowcontrol.apiserver.k8s.io/v1", text)
        self.assertEqual(result["notes"], ["2 removed-release entries read over 7d (writes only; a caller that only reads is not in this log)"])

    def test_a_removal_the_control_plane_crossed_is_a_note_not_this_upgrades_block(self):
        # The member's 1.33 control plane crossed the 1.32 removal: the API is gone and the
        # window's entries predate the crossing, so neither a blocker nor a risk, one note.
        result = removed_api_callers.evaluate({}, member(), read([]), TARGET_1_35, sample_context())
        self.assertEqual((result["blocking"], result["risks"], result["unknown"]), ([], [], []))
        self.assertIn(f"{TUNER} via legacy-flowcontrol-tuner/0.3 wrote flowcontrol.apiserver.k8s.io/v1beta3 flowschemas, removed in 1.32, which the control plane's 1.33 has crossed: the API is already gone and the writes predate the crossing, so this is not the upgrade's block", result["notes"])
        # The context's parsed master wins over the member's text: a 1.31 control plane there
        # makes the same caller this upgrade's block; a removal in (running, target] blocks.
        context = {**sample_context(), shared.CONTEXT_MASTER: (1, 31, 5, 100)}
        self.assertEqual(len(removed_api_callers.evaluate({}, member(), read([]), TARGET_1_35, context)["blocking"]), 1)
        # With neither a context master nor a member version, the target alone grades it.
        self.assertEqual(len(removed_api_callers.evaluate({}, {}, read([]), TARGET_1_35, sample_context())["blocking"]), 1)

    def test_a_caller_silent_for_more_than_48_hours_is_a_risk_naming_its_last_write(self):
        later = datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc)
        result = removed_api_callers.evaluate({}, member(**self.BEHIND), read([]), TARGET_1_35, sample_context(at=later))
        self.assertEqual(result["blocking"], [])
        self.assertEqual([r["principal"] for r in result["risks"]], [TUNER])
        self.assertIn("the last write was 2026-09-29T14:04:19.112Z, more than 48h before 2026-10-05T16:00:00Z", result["risks"][0]["detail"])
        exactly = removed_api_callers.evaluate({}, member(**self.BEHIND), read([]), TARGET_1_35, sample_context(at=datetime(2026, 10, 1, 14, 4, 19, 112000, tzinfo=timezone.utc)))
        self.assertEqual(len(exactly["blocking"]), 1)

    def test_removal_after_the_target_is_a_risk_not_a_blocker(self):
        result = removed_api_callers.evaluate({}, member(**self.BEHIND), read([]), TARGET_1_31, sample_context())
        self.assertEqual(result["blocking"], [])
        self.assertIn("removed after the target 1.31", result["risks"][0]["detail"])

    def test_without_a_target_callers_are_risks_and_the_rule_notes_it(self):
        result = removed_api_callers.evaluate({}, member(**self.BEHIND), read([]), None, sample_context())
        self.assertEqual((result["blocking"], result["unknown"]), ([], []))
        self.assertEqual(len(result["risks"]), 1)
        self.assertIn("removed in 1.32; whether this upgrade crosses it needs a target", result["risks"][0]["detail"])
        self.assertIn(removed_api_callers.NOTE_NO_TARGET, result["notes"])
        # A removal the running control plane has crossed needs no target: the same note as with
        # one, no risk, and no note claiming a risk was filed.
        result = removed_api_callers.evaluate({}, member(), read([]), None, sample_context())
        self.assertEqual((result["blocking"], result["risks"], result["unknown"]), ([], [], []))
        self.assertNotIn(removed_api_callers.NOTE_NO_TARGET, result["notes"])
        self.assertEqual(len([n for n in result["notes"] if "which the control plane's 1.33 has crossed" in n]), 1)

    def test_provider_and_unplaced_principals_never_block(self):
        removed, _ = split_sample()
        provider = with_principal(removed[0], "system:cloud-controller-manager")
        unplaced = with_principal(removed[0], "legacy-flowcontrol-tuner")
        result = removed_api_callers.evaluate({}, member(**self.BEHIND), read([]), TARGET_1_35, context_with(FakeRunner([provider, unplaced])))
        self.assertEqual(result["blocking"], [])
        by_principal = {r["principal"]: r for r in result["risks"]}
        self.assertEqual(by_principal["system:cloud-controller-manager"]["detail"], removed_api_callers.PROVIDER_DETAIL)
        self.assertIn("a class this rule cannot place (legacy-flowcontrol-tuner)", by_principal["legacy-flowcontrol-tuner"]["detail"])

    def test_a_failed_read_is_unknown_only_and_moves_the_verdict(self):
        result = removed_api_callers.evaluate({}, member(), read([]), TARGET_1_35, context_with(FakeRunner(rc=-1, stderr="timed out after 60 seconds")))
        self.assertEqual((result["blocking"], result["risks"], result["notes"]), ([], [], []))
        self.assertEqual(len(result["unknown"]), 1)
        self.assertIn("timed out after 60 seconds", result["unknown"][0]["reason"])
        self.assertEqual(status_with(removed_api_callers, result), "unknown")

    def test_a_failed_later_page_keeps_the_blocker_and_adds_an_unknown(self):
        removed, _ = split_sample()
        runner = FakeRunner(pages={audit_log.READ_REMOVED: [full_page(removed[0], tables.AUDIT_LOG_PAGE_LIMIT), FAIL]})
        result = removed_api_callers.evaluate({}, member(**self.BEHIND), read([]), TARGET_1_35, context_with(runner))
        self.assertEqual(len(result["blocking"]), 1)
        self.assertIn("page 2", result["unknown"][0]["reason"])

    def test_a_sampled_read_is_graded_and_noted_not_unknown(self):
        removed, _ = split_sample()
        pages = [full_page(removed[0], tables.AUDIT_LOG_PAGE_LIMIT, start=datetime(2026, 9, 29, 12 - i, 0, tzinfo=timezone.utc)) for i in range(3)]
        result = removed_api_callers.evaluate({}, member(**self.BEHIND), read([]), TARGET_1_35, context_with(FakeRunner(pages={audit_log.READ_REMOVED: pages})))
        self.assertEqual(len(result["blocking"]), 1)
        self.assertEqual(result["unknown"], [])
        self.assertIn("sampled 3000 entries over 3 page(s) of the removed read; more callers possible", result["notes"])

    def test_a_chatty_deprecated_writer_cannot_crowd_out_the_removed_read(self):
        # The seeded fixture's rate: a write every ten minutes is 1,008 stamped entries in
        # seven days, more than one page, all of them on the deprecated read.
        _, deprecated = split_sample()
        writer = next(e for e in deprecated if e["protoPayload"]["authenticationInfo"]["principalEmail"] == WRITER)
        context = context_with(FakeRunner(removed=[], deprecated=full_page(writer, 1008)))
        clean = removed_api_callers.evaluate({}, member(), read([]), TARGET_1_35, context)
        self.assertEqual((clean["blocking"], clean["risks"], clean["unknown"]), ([], [], []))
        self.assertEqual(clean["notes"], ["0 removed-release entries read over 7d (writes only; a caller that only reads is not in this log)"])
        noisy = deprecated_api_callers.evaluate({}, member(), read([]), TARGET_1_35, context)
        self.assertEqual([(r["principal"], r["count"]) for r in noisy["risks"]], [(WRITER, 1008)])
        self.assertEqual(noisy["unknown"], [])


class DeprecatedApiCallersTest(unittest.TestCase):
    def test_operator_callers_are_risks_and_provider_callers_are_a_note(self):
        result = deprecated_api_callers.evaluate({}, member(), read([]), TARGET_1_35, sample_context())
        self.assertEqual(result["blocking"], [])
        self.assertEqual([r["principal"] for r in result["risks"]], [GATEWAY_OPERATOR, WRITER])
        writer = result["risks"][1]
        self.assertEqual((writer["api"], writer["count"], writer["caller_class"]), ("core/v1 endpoints", 2, audit_log.CLASS_OPERATOR))
        self.assertIn("EndpointSlice", writer["successor"])
        text = deprecated_api_callers.describe(writer)
        self.assertIn(f"{WRITER} via Python-urllib/3.12 writes core/v1 endpoints (2 writes in 7d, last 2026-09-29T14:10:02.950Z)", text)
        self.assertIn("successor discovery.k8s.io/v1 EndpointSlice", text)
        self.assertIn(f"1 provider caller of deprecated APIs not filed ({ENDPOINT_CONTROLLER} on core/v1 endpoints)", result["notes"])
        self.assertNotIn(TUNER, [r["principal"] for r in result["risks"]])
        self.assertEqual(result["unknown"], [])

    def test_an_unplaced_principal_is_a_risk_that_names_it(self):
        _, deprecated = split_sample()
        writer = next(e for e in deprecated if e["protoPayload"]["authenticationInfo"]["principalEmail"] == WRITER)
        result = deprecated_api_callers.evaluate({}, member(), read([]), TARGET_1_35, context_with(FakeRunner(deprecated=[with_principal(writer, "legacy-writer")])))
        self.assertEqual([(r["principal"], r["caller_class"]) for r in result["risks"]], [("legacy-writer", audit_log.CLASS_UNPLACED)])
        self.assertEqual(result["risks"][0]["detail"], deprecated_api_callers.UNPLACED_DETAIL)

    def test_failed_read_does_not_move_the_verdict_and_a_sampled_read_is_a_note(self):
        failed = deprecated_api_callers.evaluate({}, member(), read([]), TARGET_1_35, context_with(FakeRunner(rc=1, stderr="denied")))
        self.assertEqual(failed["risks"], [])
        self.assertIn("denied", failed["unknown"][0]["reason"])
        self.assertEqual(status_with(deprecated_api_callers, failed), "ready")
        _, deprecated = split_sample()
        writer = next(e for e in deprecated if e["protoPayload"]["authenticationInfo"]["principalEmail"] == WRITER)
        pages = [full_page(writer, tables.AUDIT_LOG_PAGE_LIMIT, start=datetime(2026, 9, 29, 12 - i, 0, tzinfo=timezone.utc)) for i in range(3)]
        sampled = deprecated_api_callers.evaluate({}, member(), read([]), TARGET_1_35, context_with(FakeRunner(pages={audit_log.READ_DEPRECATED: pages})))
        self.assertEqual(len(sampled["risks"]), 1)
        self.assertEqual(sampled["unknown"], [])
        self.assertIn("sampled 3000 entries over 3 page(s) of the deprecated read; more callers possible", sampled["notes"])

    def test_clean_log_notes_it(self):
        result = deprecated_api_callers.evaluate({}, member(), read([]), TARGET_1_35, context_with(FakeRunner()))
        self.assertEqual(result["risks"], [])
        self.assertEqual(result["notes"], ["no operator caller of a deprecated API in 7d (writes only; a caller that only reads is not in this log)"])


class ClientAddonSkewTest(unittest.TestCase):
    CLUSTER = {
        "addonsConfig": {
            "httpLoadBalancing": {},
            "horizontalPodAutoscaling": {},
            "kubernetesDashboard": {"disabled": True},
            "networkPolicyConfig": {"disabled": True},
            "gcePersistentDiskCsiDriverConfig": {"enabled": True},
        }
    }
    ITEMS = [
        workload("CronJob", "seeded-shapes", "stale-kubectl-client", "registry.k8s.io/kubectl:v1.29.0"),
        workload("Deployment", "cert-manager", "cert-manager", "quay.io/jetstack/cert-manager-controller:v1.21.0"),
        workload("Deployment", "istio-system", "istiod", "docker.io/istio/pilot:1.24.2"),
        workload("Deployment", "shop", "web", "registry.k8s.io/pause:3.9"),
    ]

    def test_kubectl_outside_one_minor_is_a_risk_from_the_log_and_from_images(self):
        result = client_addon_skew.evaluate(self.CLUSTER, member(), read(self.ITEMS), TARGET_1_35, sample_context())
        self.assertEqual(result["blocking"], [])
        clients = [r for r in result["risks"] if r["kind"] == client_addon_skew.KIND_CLIENT]
        self.assertEqual(len(clients), 2)
        from_log = next(c for c in clients if "principal" in c["where"])
        self.assertEqual((from_log["version"], from_log["gap_minors"]), ("v1.29", 6))
        self.assertIn(STALE_KUBECTL, from_log["where"])
        self.assertIn("kubectl is supported within 1 minor of kube-apiserver", from_log["detail"])
        self.assertIn(tables.KUBECTL_SKEW_SOURCE, from_log["detail"])
        from_image = next(c for c in clients if "image" in c["where"])
        self.assertEqual(from_image["where"], "CronJob seeded-shapes/stale-kubectl-client (image registry.k8s.io/kubectl:v1.29.0)")
        self.assertIn("kubectl v1.29 from CronJob seeded-shapes/stale-kubectl-client", client_addon_skew.describe(from_image))
        self.assertIn("1 kubectl client within one minor of the target", result["notes"])
        self.assertIn("1 client-go caller among the deprecated-API writers the audit read returns, whose user agent carries the binary's version, not the library's; not graded", result["notes"])

    def test_one_kubectl_principal_writing_two_apis_is_one_client_naming_both(self):
        # The audit read's records are per API: the stale kubectl's ConfigMap writes plus a Secret
        # write of the same principal and user agent fold into one client, not two identical risks.
        removed, deprecated = split_sample()
        stale = next(e for e in deprecated if STALE_KUBECTL in json.dumps(e))
        secrets = json.loads(json.dumps(stale))
        secrets["insertId"] = stale["insertId"] + "-secrets"
        secrets["protoPayload"]["resourceName"] = "core/v1/namespaces/seeded-shapes/secrets/stale-client-token"
        secrets["protoPayload"]["methodName"] = "io.k8s.core.v1.secrets.patch"
        context = context_with(FakeRunner(removed, deprecated + [secrets]))
        result = client_addon_skew.evaluate(self.CLUSTER, member(), read(self.ITEMS), TARGET_1_35, context)
        from_log = [r for r in result["risks"] if r["kind"] == client_addon_skew.KIND_CLIENT and "principal" in r["where"]]
        self.assertEqual(len(from_log), 1)
        self.assertIn("2 writes to core/v1 configmaps, core/v1 secrets in ", from_log[0]["where"])
        self.assertIn("1 kubectl client within one minor of the target", result["notes"])

    def test_one_client_go_binary_writing_two_apis_is_one_caller(self):
        # The records are per API: the gateway operator's Endpoints writes plus an EndpointSlice
        # write from the same principal and user agent are one client-go caller, not two.
        removed, deprecated = split_sample()
        operator = next(e for e in deprecated if GATEWAY_OPERATOR in json.dumps(e))
        slices = json.loads(json.dumps(operator))
        slices["insertId"] = operator["insertId"] + "-slices"
        slices["protoPayload"]["resourceName"] = "discovery.k8s.io/v1beta1/namespaces/mesh/endpointslices/legacy-gateway"
        slices["protoPayload"]["methodName"] = "io.k8s.discovery.v1beta1.endpointslices.update"
        result = client_addon_skew.evaluate(self.CLUSTER, member(), read(self.ITEMS), TARGET_1_35, context_with(FakeRunner(removed, deprecated + [slices])))
        self.assertEqual([n for n in result["notes"] if "client-go" in n], ["1 client-go caller among the deprecated-API writers the audit read returns, whose user agent carries the binary's version, not the library's; not graded"])

    def test_an_addon_without_a_table_is_a_note_and_the_verdict_stays(self):
        result = client_addon_skew.evaluate(self.CLUSTER, member(), read(self.ITEMS), TARGET_1_35, context_with())
        self.assertEqual([r for r in result["risks"] if r.get("kind") == client_addon_skew.KIND_ADDON], [])
        self.assertEqual(result["unknown"], [])
        self.assertIn("istio v1.24 at Deployment istio-system/istiod (image docker.io/istio/pilot:1.24.2): version not graded (no support table with a source here; check the vendor's matrix against 1.35)", result["notes"])
        self.assertIn("cert-manager v1.21 supports the target 1.35 (https://cert-manager.io/docs/releases/)", result["notes"])
        self.assertIn("GKE add-ons on: gcePersistentDiskCsiDriverConfig, horizontalPodAutoscaling, httpLoadBalancing (GKE-managed; the cluster record carries no version, so not graded)", result["notes"])
        self.assertEqual(status_with(client_addon_skew, result), "ready")

    def test_addon_outside_its_supported_range_is_a_risk_and_an_unlisted_release_a_note(self):
        result = client_addon_skew.evaluate({}, member(), read(self.ITEMS[1:2]), (1, 37, 0, 0), context_with())
        addon = result["risks"][0]
        self.assertEqual((addon["addon"], addon["version"], addon["supported"]), ("cert-manager", "v1.21", ("1.33", "1.36")))
        self.assertIn("cert-manager v1.21 supports Kubernetes 1.33 to 1.36", client_addon_skew.describe(addon))
        self.assertIn("the target 1.37 is outside it", addon["detail"])
        old = workload("Deployment", "cert-manager", "cert-manager", "quay.io/jetstack/cert-manager-controller:v1.12.0")
        result = client_addon_skew.evaluate({}, member(), read([old]), TARGET_1_35, context_with())
        self.assertEqual((result["risks"], result["unknown"]), ([], []))
        self.assertTrue(any(n.startswith("cert-manager v1.12 at Deployment cert-manager/cert-manager (image quay.io/jetstack/cert-manager-controller:v1.12.0): version not graded (release not in the table read on 2026-10-09") for n in result["notes"]), result["notes"])

    def test_unread_objects_are_unknown_without_moving_the_verdict_and_a_missing_target_is_a_note(self):
        nothing = client_addon_skew.evaluate(self.CLUSTER, member(), read(None, error="kubectl get failed"), TARGET_1_35, sample_context())
        self.assertEqual([u["reason"] for u in nothing["unknown"]], ["cluster objects not read (kubectl get failed); kubectl images and add-ons not graded"])
        self.assertEqual(len(nothing["risks"]), 1)
        self.assertEqual(status_with(client_addon_skew, nothing), "ready")
        partial = client_addon_skew.evaluate(self.CLUSTER, member(), read(self.ITEMS[1:], read_errors={"cronjob": "forbidden", "daemonset": "forbidden"}), TARGET_1_35, context_with())
        self.assertEqual([u["reason"] for u in partial["unknown"]], ["cronjobs not read (forbidden); their kubectl images and add-ons not graded", "daemonsets not read (forbidden); their kubectl images and add-ons not graded"])
        self.assertIn("cert-manager v1.21 supports the target 1.35 (https://cert-manager.io/docs/releases/)", partial["notes"])
        no_target = client_addon_skew.evaluate(self.CLUSTER, member(), read(self.ITEMS), None, sample_context())
        self.assertEqual((no_target["risks"], no_target["unknown"]), ([], []))
        self.assertIn("no target; 3 kubectl clients not graded against a skew window", no_target["notes"])

    def test_failed_log_read_still_grades_images(self):
        result = client_addon_skew.evaluate({}, member(), read(self.ITEMS[:1]), TARGET_1_35, context_with(FakeRunner(rc=-1, stderr="timed out after 60 seconds")))
        self.assertEqual(len(result["risks"]), 1)
        self.assertIn("image registry.k8s.io/kubectl:v1.29.0", result["risks"][0]["where"])
        self.assertIn("timed out", result["unknown"][0]["reason"])

    def test_gke_addon_blocks_read_enabled_and_disabled_as_the_proto_spells_them(self):
        config = {"addonsConfig": {"dnsCacheConfig": {}, "cloudRunConfig": {"disabled": True}, "gkeBackupAgentConfig": {"enabled": True}, "configConnectorConfig": {"enabled": False}, "httpLoadBalancing": {}}}
        self.assertEqual(client_addon_skew.enabled_gke_addons(config), ["gkeBackupAgentConfig", "httpLoadBalancing"])
        self.assertEqual(client_addon_skew.enabled_gke_addons({"addonsConfig": {"dnsCacheConfig": {"enabled": True}}}), ["dnsCacheConfig"])
        self.assertEqual(client_addon_skew.enabled_gke_addons({}), [])


class ChangedDefaultsTest(unittest.TestCase):
    def test_namespace_following_latest_is_a_risk_when_a_crossed_minor_tightens(self):
        items = [psa_namespace("seeded-defaults", "baseline", "latest"), psa_namespace("unset", "restricted"), psa_namespace("pinned", "baseline", "v1.33"), psa_namespace("priv", "privileged", "latest"), namespace("plain")]
        result = changed_defaults.evaluate({}, member("1.33.5-gke.100"), read(items), TARGET_1_35, {})
        self.assertEqual(result["blocking"], [])
        self.assertEqual([r["namespace"] for r in result["risks"]], ["seeded-defaults", "unset"])
        item = result["risks"][0]
        self.assertEqual(item["setting"], "pod-security.kubernetes.io/enforce=baseline, pod-security.kubernetes.io/enforce-version=latest")
        self.assertEqual([c["minor"] for c in item["changes"]], ["1.34"])
        self.assertIn("host field of probes and lifecycle hooks must be unset", item["changes"][0]["change"])
        self.assertEqual(item["changes"][0]["source"], tables.POD_SECURITY_STANDARDS_SOURCE)
        text = changed_defaults.describe(item)
        self.assertTrue(text.startswith("namespace seeded-defaults: pod-security.kubernetes.io/enforce=baseline"))
        self.assertIn("follows the running minor, so the enforced baseline rule set changes at 1.34", text)
        self.assertEqual(result["risks"][1]["version_label"], "unset (latest)")
        self.assertIn("1 namespace pinned to a Pod Security version", result["notes"])
        self.assertIn("1 namespace enforces privileged, which no check changes", result["notes"])
        self.assertEqual(result["unknown"], [])

    def test_no_check_changes_between_the_minors_is_a_note_not_a_risk(self):
        result = changed_defaults.evaluate({}, member("1.34.2-gke.1"), read([psa_namespace("seeded-defaults", "baseline", "latest")]), TARGET_1_35, {})
        self.assertEqual(result["risks"], [])
        self.assertIn("1 namespace follows latest with no enforced check changing between 1.34 and 1.35 per the table", result["notes"])

    def test_a_widened_allowlist_is_a_note(self):
        result = changed_defaults.evaluate({}, member("1.26.0-gke.1", ("1.26.0-gke.1",)), read([psa_namespace("x", "baseline", "latest")]), (1, 27, 0, 0), {})
        self.assertEqual(result["risks"], [])
        self.assertIn("x follows latest; 1.27 only widens the baseline allowlist, which rejects nothing new", result["notes"])

    def test_restricted_only_rows_do_not_reach_a_baseline_namespace(self):
        items = [psa_namespace("base", "baseline", "latest"), psa_namespace("strict", "restricted", "latest")]
        result = changed_defaults.evaluate({}, member("1.22.0-gke.1", ("1.22.0-gke.1",)), read(items), (1, 23, 0, 0), {})
        self.assertEqual([r["namespace"] for r in result["risks"]], ["strict"])
        self.assertIn("runAsUser to 0", result["risks"][0]["changes"][0]["change"])

    def test_gitrepo_volume_is_graded_from_the_node_pools(self):
        volumes = [{"name": "src", "gitRepo": {"repository": "https://example.com/repo.git"}}]
        items = [workload("Deployment", "legacy", "git-sync", "registry.k8s.io/pause:3.9", volumes)]
        crossing = changed_defaults.evaluate({}, member("1.33.0-gke.1", ("1.32.0-gke.1",)), read(items), (1, 33, 0, 0), {})
        self.assertEqual((crossing["risks"][0]["subject"], crossing["risks"][0]["setting"]), ("Deployment legacy/git-sync", "gitRepo volume src"))
        self.assertEqual([c["minor"] for c in crossing["risks"][0]["changes"]], ["1.33"])
        self.assertIn("GitRepoVolumeDriver", crossing["risks"][0]["changes"][0]["change"])
        self.assertEqual(crossing["risks"][0]["changes"][0]["source"], tables.KUBERNETES_1_33_CHANGELOG_SOURCE)
        already_past = changed_defaults.evaluate({}, member("1.34.0-gke.1", ("1.34.0-gke.1",)), read(items), (1, 35, 0, 0), {})
        self.assertEqual(already_past["risks"], [])
        permanent = changed_defaults.evaluate({}, member("1.35.0-gke.1", ("1.35.0-gke.1",)), read(items), (1, 36, 0, 0), {})
        self.assertIn("permanently disabled", permanent["risks"][0]["changes"][0]["change"])

    def test_a_target_past_the_table_is_graded_on_the_covered_minors_with_a_note(self):
        items = [psa_namespace("seeded-defaults", "baseline", "latest")]
        result = changed_defaults.evaluate({}, member("1.33.5-gke.100"), read(items), (1, 37, 0, 0), {})
        self.assertEqual(result["unknown"], [])
        self.assertEqual([c["minor"] for c in result["risks"][0]["changes"]], ["1.34"])
        self.assertTrue(any(n.startswith("the defaults table ends at 1.36 (read 2026-10-09); minors after it up to the target 1.37 are not graded") for n in result["notes"]), result["notes"])

    def test_no_target_is_a_note_and_unread_namespaces_are_an_unknown_that_moves_nothing(self):
        items = [psa_namespace("seeded-defaults", "baseline", "latest")]
        no_target = changed_defaults.evaluate({}, member(), read(items), None, {})
        self.assertEqual((no_target["risks"], no_target["unknown"]), ([], []))
        self.assertEqual(no_target["notes"], [changed_defaults.NOTE_NO_TARGET])
        nothing = changed_defaults.evaluate({}, member(), read(None, error="kubectl get failed"), TARGET_1_35, {})
        self.assertEqual([u["reason"] for u in nothing["unknown"]], ["cluster objects not read (kubectl get failed); namespace admission labels and workload volumes not graded"])
        volumes = [{"name": "src", "gitRepo": {"repository": "https://example.com/repo.git"}}]
        partial = changed_defaults.evaluate({}, member("1.32.0-gke.1", ("1.32.0-gke.1",)), read([workload("Deployment", "legacy", "git-sync", "img", volumes)], read_errors={"namespace": "forbidden"}), (1, 33, 0, 0), {})
        self.assertEqual([u["reason"] for u in partial["unknown"]], ["namespaces not read (forbidden); admission labels not graded"])
        self.assertEqual(len(partial["risks"]), 1)
        self.assertEqual(status_with(changed_defaults, partial), "ready")
        bad = changed_defaults.evaluate({}, member("latest"), read(items), TARGET_1_35, {})
        self.assertIn("unparsable", bad["unknown"][0]["reason"])

    def test_crossed_minors(self):
        self.assertEqual(changed_defaults.crossed_minors((1, 33), (1, 35)), [(1, 34)])
        self.assertEqual(changed_defaults.crossed_minors((1, 32), (1, 33)), [(1, 33)])
        self.assertEqual(changed_defaults.crossed_minors((1, 35), (1, 35)), [])
        self.assertEqual(changed_defaults.crossed_minors(None, (1, 23)), [(1, 23)])


class ReportTest(unittest.TestCase):
    """The four rules through `fleet_upgrade_report.main`: the fold, the verdict and the table."""

    TARGET = "1.35.1-gke.1000"
    AT = "2026-09-29T16:00:00Z"

    def setUp(self):
        self.state_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.state_dir, True)
        self.kubeconfig_dir = os.path.join(tempfile.mkdtemp(), "kubeconfigs")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.kubeconfig_dir), True)
        # Both members sit one minor below the target, inside the skew window: seeded-a is the
        # audit subject, served a removal at 1.34, in (running, target]; the sample's own 1.32
        # would be a crossing on a 1.33 control plane and grade as a note.
        subject = cluster("seeded-a", "us-central1-a", "1.33.5-gke.100", [("default-pool", "1.33.5-gke.100")])
        behind = cluster("seeded-c", "us-central1-a", "1.33.5-gke.100", [("default-pool", "1.33.5-gke.100")])
        self.clusters = {"p1": [subject, behind]}
        # The target sits above the fleet: the location's server config lists it as valid beside the
        # running version, as GKE lists a version it serves, so `target-patch-valid` has nothing to say.
        versions = ["1.33.5-gke.100", self.TARGET]
        self.config = {"us-central1-a": {"channels": [{"channel": "REGULAR", "defaultVersion": "1.33.5-gke.100", "validVersions": versions}], "validMasterVersions": versions, "validNodeVersions": versions}}
        self.objects = {
            "seeded-a": [k8s_workload("Deployment", "shop", "web", 3, {"app": "web"}), k8s_pdb("shop", "web-pdb", {"maxUnavailable": 1, "selector": {"matchLabels": {"app": "web"}}}, 3)],
            "seeded-c": [k8s_workload("Deployment", "shop", "web", 3, {"app": "web"})],
        }
        self.namespaced = {"seeded-c": [psa_namespace("seeded-defaults", "baseline", "latest"), workload("CronJob", "seeded-shapes", "stale-kubectl-client", "registry.k8s.io/kubectl:v1.29.0")]}

    @staticmethod
    def served(release="1.34"):
        """The sample with its removed-release stamps moved to `release`."""
        entries = sample_entries()
        for entry in entries:
            if audit_log.LABEL_REMOVED_RELEASE in entry.get("labels", {}):
                entry["labels"][audit_log.LABEL_REMOVED_RELEASE] = release
        return entries

    def _run(self, fake, *extra):
        out_path = os.path.join(tempfile.mkdtemp(), "report.json")
        stdout = io.StringIO()
        with patch.object(report, "run_cmd", fake), redirect_stdout(stdout):
            rc = report.main(["--state-dir", self.state_dir, "--project", "p1", "--target-version", self.TARGET, "--readiness", "--at", self.AT, "--kubeconfig-dir", self.kubeconfig_dir, "--output", out_path, *extra])
        with open(out_path, encoding="utf-8") as f:
            data = json.load(f)
        return rc, stdout.getvalue(), data

    def test_a_recent_removed_api_caller_blocks_and_the_rest_are_risks_in_the_two_columns(self):
        fake = FakeReadinessCommands(self.clusters, self.config, self.objects, namespaced_objects_by_cluster=self.namespaced, audit_by_cluster={"seeded-a": self.served()})
        rc, text, data = self._run(fake)
        self.assertEqual(rc, report.EXIT_OK)
        by_name = {m["cluster"]: m["readiness"] for m in data["members"]}
        a = by_name["seeded-a"]
        self.assertEqual(a["status"], "blocked")
        self.assertEqual([(f["rule"], f["principal"]) for f in a["rule_blocking"]], [("removed-api-callers", TUNER)])
        self.assertEqual({f["rule"] for f in a["risks"]}, {"deprecated-api-callers", "client-addon-skew", "zonal-control-plane", "known-issues-matrix"})
        self.assertEqual((a["audit_log"]["removed"]["entries"], a["audit_log"]["deprecated"]["entries"]), (2, 6))
        self.assertEqual(a["rules"]["removed-api-callers"]["blocking"], 1)
        c = by_name["seeded-c"]
        self.assertEqual(c["status"], "ready")
        self.assertEqual({(f["rule"], f.get("namespace") or f.get("version")) for f in c["risks"] if f["rule"] not in ("zonal-control-plane", "known-issues-matrix")}, {("changed-defaults", "seeded-defaults"), ("client-addon-skew", "v1.29")})
        self.assertEqual(data["readiness"]["summary"], {"blocked": 1, "ready": 1, "unknown": 0})
        self.assertEqual(data["readiness"]["members_with_risks"], 2)
        row = next(l for l in text.splitlines() if l.startswith("| p1 | seeded-a |") and "| blocked |" in l)
        self.assertIn(f"{TUNER} via legacy-flowcontrol-tuner/0.3 writes flowcontrol.apiserver.k8s.io/v1beta3 flowschemas, removed in 1.34", row)
        self.assertIn(f"{WRITER} via Python-urllib/3.12 writes core/v1 endpoints", row)
        self.assertIn("kubectl v1.29 from principal " + STALE_KUBECTL, row)
        self.assertIn("deprecated-api-callers: 1 provider caller of deprecated APIs not filed", row)
        c_row = next(l for l in text.splitlines() if l.startswith("| p1 | seeded-c |") and "| ready |" in l)
        self.assertIn("namespace seeded-defaults: pod-security.kubernetes.io/enforce=baseline", c_row)
        self.assertIn("kubectl v1.29 from CronJob seeded-shapes/stale-kubectl-client", c_row)

    def test_a_removed_api_caller_silent_for_six_days_is_a_risk_and_the_member_ready(self):
        fake = FakeReadinessCommands(self.clusters, self.config, self.objects, audit_by_cluster={"seeded-a": self.served()})
        rc, text, data = self._run(fake, "--at", "2026-10-05T16:00:00Z")
        a = next(m for m in data["members"] if m["cluster"] == "seeded-a")["readiness"]
        self.assertEqual(a["status"], "ready")
        self.assertEqual(a["rule_blocking"], [])
        stale = next(f for f in a["risks"] if f["rule"] == "removed-api-callers")
        self.assertIn("more than 48h before 2026-10-05T16:00:00Z", stale["detail"])
        # The sample's own 1.32 removal on this 1.33 control plane is a crossing: a note, and the member ready.
        fake = FakeReadinessCommands(self.clusters, self.config, self.objects, audit_by_cluster={"seeded-a": sample_entries()})
        rc, text, data = self._run(fake)
        a = next(m for m in data["members"] if m["cluster"] == "seeded-a")["readiness"]
        self.assertEqual((a["status"], a["rule_blocking"], [f["rule"] for f in a["risks"] if f["rule"] == "removed-api-callers"]), ("ready", [], []))
        self.assertIn("removed in 1.32, which the control plane's 1.33 has crossed", a["note"])

    def test_a_failed_audit_read_is_unknown_for_the_member_through_the_blocking_rule(self):
        fake = FakeReadinessCommands(self.clusters, self.config, self.objects, failing_logging=["seeded-a"])
        rc, text, data = self._run(fake)
        self.assertEqual(rc, report.EXIT_PARTIAL)
        a = next(m for m in data["members"] if m["cluster"] == "seeded-a")["readiness"]
        self.assertEqual(a["status"], "unknown")
        reasons = {u["rule"]: u for u in a["unknown"]}
        self.assertTrue(reasons["removed-api-callers"]["can_block"])
        self.assertFalse(reasons["deprecated-api-callers"]["can_block"])
        self.assertIn("timed out after 60 seconds", reasons["removed-api-callers"]["reason"])
        self.assertIn("removed-api-callers: removed-API callers: `gcloud logging read` (removed read, page 1) failed (rc=-1): timed out after 60 seconds", a["note"])
        self.assertEqual(next(m for m in data["members"] if m["cluster"] == "seeded-c")["readiness"]["status"], "ready")
        # The failed read is listed apart from the kubectl read failures, under the readiness
        # block, and printed under the table; `errors` carries no entry for it.
        failures = data["readiness"]["audit_log_failures"]
        self.assertEqual({(f["cluster"], f["read"]) for f in failures}, {("seeded-a", audit_log.READ_REMOVED), ("seeded-a", audit_log.READ_DEPRECATED)})
        self.assertTrue(all("timed out after 60 seconds" in f["message"] for f in failures))
        self.assertEqual(data["errors"], [])
        self.assertIn("- audit log not read for p1 (us-central1-a) cluster seeded-a (removed read): `gcloud logging read` (removed read, page 1) failed (rc=-1): timed out after 60 seconds", text)


if __name__ == "__main__":
    unittest.main()
