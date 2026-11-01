#!/usr/bin/env python3
"""Unit tests for workload_triage.py: fixture objects, kubectl stubbed."""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import workload_triage  # noqa: E402

NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)


def stamp(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def pod(name, namespace="shop", phase="Running", ready=True, age=120, waiting=None, last_term=None, restarts=0, scheduled=None, owner="ReplicaSet"):
    cs = {"name": "app", "ready": ready, "restartCount": restarts, "state": {}}
    if waiting:
        cs["state"] = {"waiting": {"reason": waiting}}
    else:
        cs["state"] = {"running": {}}
    if last_term:
        cs["lastState"] = {"terminated": last_term}
    conditions = [{"type": "Ready", "status": "True" if ready else "False"}]
    if scheduled is not None:
        conditions.append({"type": "PodScheduled", "status": "False", "reason": scheduled})
    status = {"phase": phase, "conditions": conditions}
    if phase != "Pending" or waiting:
        status["containerStatuses"] = [cs]
    meta = {"name": name, "namespace": namespace, "creationTimestamp": stamp(age)}
    if owner:
        meta["ownerReferences"] = [{"kind": owner, "name": f"{name}-owner"}]
    return {
        "metadata": meta,
        "spec": {"containers": [{"name": "app"}]},
        "status": status,
    }


def deploy(name, namespace="shop", replicas=2, ready=2, kind="Deployment"):
    if kind == "DaemonSet":
        status = {"desiredNumberScheduled": replicas, "numberReady": ready, "updatedNumberScheduled": replicas}
        spec = {}
    else:
        status = {"readyReplicas": ready, "updatedReplicas": replicas}
        spec = {"replicas": replicas}
    return {"kind": kind, "metadata": {"name": name, "namespace": namespace}, "spec": spec, "status": status}


def job_obj(
    name,
    namespace="shop",
    complete=True,
    failed=False,
    failed_reason=None,
    succeeded=None,
    completions=None,
    active=None,
    failed_count=None,
):
    conditions = []
    if complete:
        conditions.append({"type": "Complete", "status": "True"})
    if failed:
        cond = {"type": "Failed", "status": "True"}
        if failed_reason is not None:
            cond["reason"] = failed_reason
        conditions.append(cond)
    status = {}
    if conditions:
        status["conditions"] = conditions
    if succeeded is not None:
        status["succeeded"] = succeeded
    if active is not None:
        status["active"] = active
    if failed_count is not None:
        status["failed"] = failed_count
    spec = {}
    if completions is not None:
        spec["completions"] = completions
    return {"kind": "Job", "metadata": {"name": name, "namespace": namespace}, "spec": spec, "status": status}


def event(obj, reason, minutes_ago, namespace="shop", kind="Pod", count=1, message="msg"):
    return {
        "metadata": {"namespace": namespace},
        "involvedObject": {"kind": kind, "name": obj, "namespace": namespace},
        "reason": reason,
        "lastTimestamp": stamp(minutes_ago),
        "count": count,
        "message": message,
    }


def stub(pods=(), controllers=(), events=(), fail=None):
    calls = []

    def run(args):
        calls.append(args)
        if fail and fail in args:
            return 1, "", "forbidden"
        if args == workload_triage.PODS_ARGS:
            items = pods
        elif args == workload_triage.CONTROLLERS_ARGS:
            items = controllers
        elif args == workload_triage.EVENTS_ARGS:
            items = events
        else:
            raise AssertionError(f"unexpected kubectl call {args}")
        return 0, json.dumps({"items": list(items)}), ""

    run.calls = calls
    return run


def triage(**kw):
    include = kw.pop("include_system", False)
    return workload_triage.triage(stub(**kw), NOW, include, 10, 60, context="test-context")


class ClassifyPodTest(unittest.TestCase):
    def test_healthy_running_pod_is_not_reported(self):
        report = triage(pods=[pod("web")])
        self.assertEqual(report["summary"]["unhealthy"], 0)
        self.assertEqual(report["summary"]["healthy"], 1)

    def test_crashloop_reports_last_exit_and_hands_to_crashloop_skill(self):
        term = {"exitCode": 137, "reason": "OOMKilled", "finishedAt": stamp(3)}
        report = triage(pods=[pod("api", ready=False, waiting="CrashLoopBackOff", last_term=term, restarts=7)])
        row = report["unhealthy_pods"][0]
        self.assertEqual(row["status"], "CrashLoopBackOff")
        self.assertEqual(row["last_exit"], "137 OOMKilled")
        self.assertEqual(row["restarts"], 7)
        self.assertEqual(row["skill"], "pod-crashloop-oom")

    def test_image_pull_and_config_errors_go_to_container_creating(self):
        report = triage(
            pods=[
                pod("a", ready=False, waiting="ImagePullBackOff"),
                pod("b", ready=False, waiting="CreateContainerConfigError"),
            ]
        )
        self.assertEqual({r["skill"] for r in report["unhealthy_pods"]}, {"pod-container-creating"})

    def test_unschedulable_old_pod_goes_to_pending_skill(self):
        report = triage(pods=[pod("p", phase="Pending", ready=False, scheduled="Unschedulable", age=60)])
        self.assertEqual(report["unhealthy_pods"][0]["skill"], "pod-pending-scheduling")

    def test_scheduling_gated_pod_past_grace_is_initializing_not_unhealthy(self):
        gated_cond = pod("queued-1", phase="Pending", ready=False, scheduled="SchedulingGated", age=180)
        gated_spec = pod("queued-2", phase="Pending", ready=False, age=180)
        gated_spec["spec"]["schedulingGates"] = [{"name": "kueue.x-k8s.io/admission"}]
        report = triage(pods=[gated_cond, gated_spec])
        self.assertEqual(report["summary"]["unhealthy"], 0)
        self.assertEqual(report["unhealthy_pods"], [])
        self.assertEqual(report["summary"]["initializing"], 2)
        self.assertEqual([(r["pod"], r["reason"], r["skill"]) for r in report["initializing"]], [
            ("queued-1", "SchedulingGated", ""),
            ("queued-2", "SchedulingGated", ""),
        ])

    def test_terminating_pod_past_grace_is_initializing_not_unhealthy(self):
        departing = pod("web-old", phase="Running", ready=False, age=300)
        departing["metadata"]["deletionTimestamp"] = stamp(1)
        report = triage(pods=[departing, pod("web-new")])
        self.assertEqual(report["summary"]["unhealthy"], 0)
        self.assertEqual(report["unhealthy_pods"], [])
        self.assertEqual(report["summary"]["healthy"], 1)
        self.assertEqual(report["summary"]["initializing"], 1)
        self.assertEqual(report["initializing"][0]["reason"], "Terminating")
        self.assertEqual(report["initializing"][0]["skill"], "")

    def test_young_starting_pods_are_initializing_not_unhealthy(self):
        report = triage(
            pods=[
                pod("new1", phase="Pending", ready=False, scheduled="Unschedulable", age=2),
                pod("new2", phase="Pending", ready=False, waiting="ContainerCreating", age=3),
                pod("new3", ready=False, age=1),
            ]
        )
        self.assertEqual(report["summary"]["unhealthy"], 0)
        self.assertEqual(report["summary"]["initializing"], 3)
        self.assertEqual({r["status"] for r in report["initializing"]}, {"initializing"})

    def test_stuck_container_creating_past_grace_is_unhealthy(self):
        report = triage(pods=[pod("old", phase="Pending", ready=False, waiting="ContainerCreating", age=30)])
        self.assertEqual(report["unhealthy_pods"][0]["skill"], "pod-container-creating")

    def test_recent_restart_on_ready_pod_is_reported_old_restart_is_not(self):
        recent = {"exitCode": 1, "reason": "Error", "finishedAt": stamp(20)}
        old = {"exitCode": 1, "reason": "Error", "finishedAt": stamp(600)}
        report = triage(pods=[pod("flaky", last_term=recent, restarts=1), pod("settled", last_term=old, restarts=4)])
        self.assertEqual([r["pod"] for r in report["unhealthy_pods"]], ["flaky"])
        self.assertEqual(report["unhealthy_pods"][0]["status"], "RecentRestart")
        self.assertEqual(report["summary"]["unhealthy"], 1)
        earlier = report["restarted_earlier"]
        self.assertEqual([(r["pod"], r["restarts"], r["last_restart"]) for r in earlier], [("settled", 4, "10h ago")])
        self.assertEqual(earlier[0]["skill"], "")

    def test_succeeded_pods_are_ignored_and_failed_pods_reported(self):
        done = pod("job-1", phase="Succeeded", ready=False)
        failed = pod("job-2", phase="Failed", ready=False, age=20)
        failed["status"]["reason"] = "Evicted"
        report = triage(pods=[done, failed])
        self.assertEqual(report["summary"]["pods"], 1)
        self.assertEqual(report["unhealthy_pods"][0]["status"], "Evicted")
        self.assertEqual(report["summary"]["unhealthy"], 1)

    def test_pods_that_failed_before_the_window_are_history_not_unhealthy(self):
        old = pod("evicted", phase="Failed", ready=False, age=600)
        old["status"]["reason"] = "Evicted"
        term = {"exitCode": 1, "reason": "Error", "finishedAt": stamp(30)}
        recent = pod("job-run", phase="Failed", ready=False, age=600, last_term=term)
        report = triage(pods=[old, recent, pod("web")])
        self.assertEqual([r["pod"] for r in report["stale_failed"]], ["evicted"])
        self.assertEqual([r["pod"] for r in report["unhealthy_pods"]], ["job-run"])
        self.assertEqual(report["summary"]["healthy"], 1)
        self.assertEqual(report["summary"]["unhealthy"], 1)

    def test_old_failure_with_no_replacing_controller_stays_unhealthy(self):
        job = pod("nightly-1", phase="Failed", ready=False, age=600, owner="Job")
        bare = pod("one-off", phase="Failed", ready=False, age=600, owner=None)
        report = triage(pods=[job, bare])
        self.assertEqual(report["stale_failed"], [])
        self.assertEqual(sorted(r["pod"] for r in report["unhealthy_pods"]), ["nightly-1", "one-off"])

    def test_retained_failed_pod_of_completed_job_is_history_not_unhealthy(self):
        attempt_1 = pod("migrate-try1", phase="Failed", ready=False, age=15, owner="Job")
        attempt_1["metadata"]["ownerReferences"] = [{"kind": "Job", "name": "migrate"}]
        attempt_2 = pod("migrate-try2", phase="Succeeded", ready=False, age=12, owner="Job")
        attempt_2["metadata"]["ownerReferences"] = [{"kind": "Job", "name": "migrate"}]
        by_count = pod("sync-try1", phase="Failed", ready=False, age=400, owner="Job")
        by_count["metadata"]["ownerReferences"] = [{"kind": "Job", "name": "sync"}]
        report = triage(
            pods=[attempt_1, attempt_2, by_count, pod("web")],
            controllers=[
                job_obj("migrate", complete=True),
                job_obj("sync", complete=False, succeeded=1),
            ],
        )
        self.assertEqual(report["summary"]["unhealthy"], 0)
        self.assertEqual(report["controllers_below_desired"], [])
        self.assertEqual(sorted(r["pod"] for r in report["stale_failed"]), ["migrate-try1", "sync-try1"])

    def test_failed_or_incomplete_job_keeps_its_failed_pod_unhealthy(self):
        exhausted = pod("etl-1", phase="Failed", ready=False, age=300, owner="Job")
        exhausted["metadata"]["ownerReferences"] = [{"kind": "Job", "name": "etl"}]
        partial = pod("shard-1", phase="Failed", ready=False, age=300, owner="Job")
        partial["metadata"]["ownerReferences"] = [{"kind": "Job", "name": "shards"}]
        report = triage(
            pods=[exhausted, partial],
            controllers=[
                job_obj("etl", complete=False, failed=True, failed_reason="BackoffLimitExceeded", succeeded=0),
                job_obj("shards", complete=False, succeeded=1, completions=3),
            ],
        )
        self.assertEqual(report["stale_failed"], [])
        self.assertEqual(sorted(r["pod"] for r in report["unhealthy_pods"]), ["etl-1", "shard-1"])
        self.assertEqual([r["controller"] for r in report["controllers_below_desired"]], ["Job/etl"])
        self.assertEqual(report["summary"]["unhealthy_pods"], 2)
        self.assertEqual(report["summary"]["controllers_below_desired"], 1)
        self.assertEqual(report["summary"]["unhealthy"], 3)

    def test_eviction_without_finished_at_is_dated_by_its_conditions(self):
        evicted = pod("evicted-now", phase="Failed", ready=False, age=600)
        evicted["status"]["reason"] = "Evicted"
        evicted["status"]["conditions"].append({"type": "DisruptionTarget", "status": "True", "lastTransitionTime": stamp(5)})
        report = triage(pods=[evicted])
        self.assertEqual([r["pod"] for r in report["unhealthy_pods"]], ["evicted-now"])

    def test_completed_init_container_is_not_the_last_exit(self):
        p = pod("web", last_term={"exitCode": 1, "reason": "Error", "finishedAt": stamp(600)}, restarts=2)
        p["status"]["initContainerStatuses"] = [
            {"name": "init", "ready": True, "restartCount": 0, "state": {"terminated": {"exitCode": 0, "reason": "Completed", "finishedAt": stamp(30)}}}
        ]
        report = triage(pods=[p])
        self.assertEqual(report["unhealthy_pods"], [])
        self.assertEqual(report["restarted_earlier"][0]["last_exit"], "1 Error")


class ControllerTest(unittest.TestCase):
    def test_only_controllers_below_desired_are_listed(self):
        report = triage(
            controllers=[
                deploy("ok"),
                deploy("short", replicas=3, ready=1),
                deploy("scaled-down", replicas=0, ready=0),
                deploy("ds", replicas=4, ready=3, kind="DaemonSet"),
                job_obj("batch-ok", complete=True),
            ]
        )
        self.assertEqual(
            [r["controller"] for r in report["controllers_below_desired"]],
            ["Deployment/short", "DaemonSet/ds"],
        )
        self.assertEqual(report["summary"]["unhealthy"], 2)

    def test_failed_job_with_deleted_pods_reports_controller_below_desired(self):
        report = triage(
            pods=[],
            controllers=[
                job_obj("nightly", complete=False, failed=True, failed_reason="DeadlineExceeded", succeeded=0),
                job_obj("retrying", complete=False, failed=False, succeeded=0, failed_count=1, active=0),
                job_obj("still-running", complete=False, failed=True, failed_reason="BackoffLimitExceeded", active=1),
            ],
        )
        self.assertEqual(
            report["controllers_below_desired"],
            [
                {
                    "namespace": "shop",
                    "controller": "Job/nightly",
                    "desired": 1,
                    "ready": 0,
                    "updated": 0,
                    "reason": "DeadlineExceeded",
                }
            ],
        )
        self.assertEqual(report["summary"]["controllers_below_desired"], 1)
        self.assertEqual(report["summary"]["unhealthy"], 1)
        rendered = workload_triage.render_text(report)
        self.assertIn("Job/nightly", rendered)
        self.assertIn("failed: DeadlineExceeded", rendered)
        self.assertTrue(rendered.rstrip().endswith("unhealthy: 1\nsystem unhealthy: 0"))


class EventsTest(unittest.TestCase):
    def test_events_outside_window_are_dropped_and_repeats_grouped(self):
        report = triage(
            events=[
                event("api", "BackOff", 5, count=3),
                event("api", "BackOff", 2, count=2, message="newest"),
                event("api", "FailedMount", 120),
            ]
        )
        rows = report["warning_events"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["count"], 5)
        self.assertEqual(rows[0]["message"], "newest")
        self.assertEqual(report["summary"]["warning_events"], 5)

    def test_node_events_are_folded_into_one_summary(self):
        report = triage(events=[event("n1", "KubeletStart", 5, namespace="default", kind="Node")])
        self.assertEqual(report["warning_events"], [])
        self.assertEqual(report["node_warning_events"]["count"], 1)

    def test_warning_events_carry_current_object_state_and_render_state_column(self):
        evicted_old = pod("evicted-old", phase="Failed", ready=False, age=600)
        evicted_old["status"]["reason"] = "Evicted"
        report = triage(
            pods=[
                pod("healthy-web"),
                pod("starting-web", phase="Pending", ready=False, scheduled="Unschedulable", age=2),
                evicted_old,
                pod("crashing-api", ready=False, waiting="CrashLoopBackOff"),
            ],
            controllers=[
                deploy("short-dep", replicas=2, ready=1),
                deploy("ok-dep", replicas=2, ready=2),
            ],
            events=[
                event("healthy-web", "FailedScheduling", 20),
                event("starting-web", "FailedScheduling", 1),
                event("evicted-old", "Evicted", 50),
                event("crashing-api", "BackOff", 3),
                event("vanished-pod", "FailedMount", 10),
                event("short-dep", "FailedCreate", 5, kind="Deployment"),
                event("ok-dep", "ScalingReplicaSet", 15, kind="Deployment"),
                event("data-0", "ProvisioningFailed", 8, kind="PersistentVolumeClaim"),
            ],
        )
        by_object = {r["object"]: r["state"] for r in report["warning_events"]}
        self.assertEqual(
            by_object,
            {
                "Pod/healthy-web": "healthy",
                "Pod/starting-web": "initializing",
                "Pod/evicted-old": "stale_failed",
                "Pod/crashing-api": "unhealthy",
                "Pod/vanished-pod": "gone",
                "Deployment/short-dep": "unhealthy",
                "Deployment/ok-dep": "healthy",
                "PersistentVolumeClaim/data-0": "-",
            },
        )
        rendered = workload_triage.render_text(report)
        self.assertIn("STATE", rendered)
        self.assertIn("Pod/healthy-web", rendered)
        self.assertIn("Pod/vanished-pod", rendered)


class SystemNamespaceTest(unittest.TestCase):
    def test_system_namespaces_are_summarised_not_listed(self):
        term = {"exitCode": 1, "reason": "Error", "finishedAt": stamp(3)}
        report = triage(
            pods=[
                pod("netd", namespace="kube-system", ready=False, waiting="CrashLoopBackOff", last_term=term),
                pod("collector", namespace="gmp-system"),
                pod("web"),
            ],
            events=[event("netd", "BackOff", 3, namespace="kube-system")],
        )
        self.assertEqual(report["summary"]["unhealthy"], 0)
        self.assertEqual(report["summary"]["system_unhealthy"], 1)
        self.assertEqual(report["summary"]["pods"], 1)
        self.assertEqual(report["system_namespaces"]["kube-system"]["unhealthy_pods"], 1)
        self.assertNotIn("unhealthy", report["system_namespaces"]["kube-system"])
        self.assertEqual(json.dumps(report).count('"unhealthy":'), 1)
        self.assertIn("gmp-system", report["system_namespaces_healthy"])

    def test_folded_namespace_crashes_report_system_unhealthy_count(self):
        term = {"exitCode": 1, "reason": "Error", "finishedAt": stamp(2)}
        run = stub(
            pods=[
                pod("invoice-worker", namespace="billing-system", ready=False, waiting="CrashLoopBackOff", last_term=term),
                pod("kube-dns-abc", namespace="kube-system", ready=False, waiting="CrashLoopBackOff", last_term=term),
                pod("web"),
            ],
            controllers=[deploy("kube-dns", namespace="kube-system", replicas=2, ready=1)],
        )
        out = io.StringIO()
        with redirect_stdout(out):
            code = workload_triage.main([], run=run, now=NOW, context="test-context")
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("billing-system: 1/1 pods unhealthy (CrashLoopBackOff 1), 0 controllers below desired", text)
        self.assertIn("kube-system: 1/1 pods unhealthy (CrashLoopBackOff 1), 1 controllers below desired", text)
        self.assertTrue(text.rstrip().endswith("unhealthy: 0\nsystem unhealthy: 3"), text)

    def test_system_restarts_ride_on_the_summary_line(self):
        old = {"exitCode": 1, "reason": "Error", "finishedAt": stamp(300)}
        run = stub(pods=[pod("netd-x", namespace="kube-system", last_term=old, restarts=2), pod("web")])
        out = io.StringIO()
        with redirect_stdout(out):
            workload_triage.main([], run=run, now=NOW, context="test-context")
        text = out.getvalue()
        self.assertIn("kube-system: 0/1 pods unhealthy", text)
        self.assertIn("restarted pods: netd-x 2x, last 5h ago", text)
        self.assertTrue(text.rstrip().endswith("unhealthy: 0\nsystem unhealthy: 0"))

    def test_system_restart_in_window_is_named_first_despite_the_cap(self):
        boot = {"exitCode": 255, "reason": "Unknown", "finishedAt": stamp(3000)}
        pods = [pod(f"sys-{i}", namespace="kube-system", last_term=boot, restarts=9) for i in range(7)]
        recent = {"exitCode": 1, "reason": "Error", "finishedAt": stamp(10)}
        pods.append(pod("netd-new", namespace="kube-system", last_term=recent, restarts=1))
        out = io.StringIO()
        with redirect_stdout(out):
            workload_triage.main([], run=stub(pods=pods), now=NOW, context="test-context")
        line = [ln for ln in out.getvalue().splitlines() if ln.startswith("kube-system:")][0]
        self.assertIn("1/8 pods unhealthy (RecentRestart 1)", line)
        self.assertIn("restarted pods: netd-new 1x, last 10m ago (in window), sys-", line)
        self.assertIn("(+3 more; --include-system lists them)", line)

    def test_crashlooping_system_pod_ranks_ahead_of_older_in_window_ready_restarts(self):
        pods = [
            pod(
                f"ready-{i}",
                namespace="kube-system",
                last_term={"exitCode": 1, "reason": "Error", "finishedAt": stamp(5 + 10 * i)},
                restarts=2,
            )
            for i in range(6)
        ]
        pods.append(
            pod(
                "crash-now",
                namespace="kube-system",
                ready=False,
                waiting="CrashLoopBackOff",
                last_term={"exitCode": 137, "reason": "OOMKilled", "finishedAt": stamp(1)},
                restarts=4,
            )
        )
        out = io.StringIO()
        with redirect_stdout(out):
            workload_triage.main([], run=stub(pods=pods), now=NOW, context="test-context")
        line = [ln for ln in out.getvalue().splitlines() if ln.startswith("kube-system:")][0]
        self.assertIn("7/7 pods unhealthy (CrashLoopBackOff 1, RecentRestart 6)", line)
        self.assertIn(
            "restarted pods: crash-now 4x, last 1m ago (in window), ready-0 2x, last 5m ago (in window),",
            line,
        )
        self.assertIn("(+2 more; --include-system lists them)", line)

    def test_include_system_lists_them(self):
        report = triage(
            pods=[pod("netd", namespace="kube-system", ready=False, waiting="CrashLoopBackOff")],
            include_system=True,
        )
        self.assertEqual(report["summary"]["unhealthy"], 1)
        self.assertEqual(report["summary"]["system_unhealthy"], 0)

    def test_namespace_rules(self):
        for ns in ("kube-system", "gke-managed-cim", "gmp-public", "cnrm-system"):
            self.assertTrue(workload_triage.is_system_namespace(ns), ns)
        for ns in ("default", "prod", "systemd-tools"):
            self.assertFalse(workload_triage.is_system_namespace(ns), ns)


class CurrentContextTest(unittest.TestCase):
    def test_reads_current_context_locally_from_kubeconfig_without_kubectl(self):
        with tempfile.TemporaryDirectory() as tmp:
            kc = Path(tmp) / "kubeconfig.yaml"
            kc.write_text("apiVersion: v1\ncurrent-context: gke_proj_us-central1_prod\n", encoding="utf-8")
            self.assertEqual(
                workload_triage.current_context(env={"KUBECONFIG": str(kc)}, home=Path(tmp)),
                "gke_proj_us-central1_prod",
            )

    def test_kubeconfig_path_list_picks_first_defined_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "empty.yaml"
            first.write_text("apiVersion: v1\ncontexts: []\n", encoding="utf-8")
            second = Path(tmp) / "pinned.yaml"
            second.write_text("apiVersion: v1\ncurrent-context: gke_p_l_c\n", encoding="utf-8")
            merged = os.pathsep.join([str(first), str(second)])
            self.assertEqual(
                workload_triage.current_context(env={"KUBECONFIG": merged}, home=Path(tmp)),
                "gke_p_l_c",
            )

    def test_falls_back_to_default_home_kubeconfig_and_unknown_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertEqual(workload_triage.current_context(env={}, home=home), "unknown")
            default_kc = home / ".kube" / "config"
            default_kc.parent.mkdir(parents=True)
            default_kc.write_text("current-context: home-cluster\n", encoding="utf-8")
            self.assertEqual(workload_triage.current_context(env={}, home=home), "home-cluster")

    def test_yaml_fallback_matches_credential_proxy_client_on_hashes_folded_and_multidoc(self):
        from credential_proxy_client import read_current_context

        cases = [
            ("apiVersion: v1\ncurrent-context: gke_p_us-central1_c#1\n", "gke_p_us-central1_c#1"),
            ("apiVersion: v1\ncurrent-context: 'a#b'\n", "a#b"),
            ("apiVersion: v1\ncurrent-context: >-\n  folded-ctx\n", "folded-ctx"),
            ("apiVersion: v1\ncurrent-context: first-doc\n---\ncurrent-context: second-doc\n", None),
            ("apiVersion: v1\ncurrent-context: [not, a, string]\n", None),
        ]
        for text, expected in cases:
            self.assertEqual(read_current_context(text), expected, text)
            with patch.dict(sys.modules, {"credential_proxy_client": None}):
                self.assertEqual(workload_triage._parse_current_context(text), expected, text)

    def test_returns_unknown_when_yaml_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            kc = Path(tmp) / "kubeconfig.yaml"
            kc.write_text("apiVersion: v1\ncurrent-context: 'a#b'\n", encoding="utf-8")
            for blocked in ({"yaml": None}, {"credential_proxy_client": None, "yaml": None}):
                with patch.dict(sys.modules, blocked):
                    self.assertIsNone(workload_triage._parse_current_context("current-context: 'a#b'\n"))
                    self.assertEqual(
                        workload_triage.current_context(env={"KUBECONFIG": str(kc)}, home=Path(tmp)),
                        "unknown",
                    )


class ProxyTruncationTest(unittest.TestCase):
    def run_main(self, argv, run, **kw):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = workload_triage.main(argv, run=run, now=NOW, context="test-context", **kw)
        return code, out.getvalue(), err.getvalue()

    def test_truncated_all_namespaces_reads_recover_across_populated_namespaces_only(self):
        cut = '{"apiVersion":"v1","kind":"List","items":[{"metadata":{"name":"cut'
        shop_pod = pod("api", namespace="shop", ready=False, waiting="CrashLoopBackOff")
        sys_pod = pod("netd", namespace="kube-system")
        short_dep = deploy("api", namespace="shop", replicas=2, ready=1)
        warn_ev = event("api", "BackOff", 4, namespace="shop", count=3)
        calls = []

        def run(args):
            calls.append(args)
            if args in (workload_triage.PODS_ARGS, workload_triage.CONTROLLERS_ARGS, workload_triage.EVENTS_ARGS):
                return 0, cut, "credential proxy output truncated\n"
            if args == ("get", "pods", "--all-namespaces", *workload_triage.NAMESPACE_PROJECTION_ARGS):
                return 0, "shop\nkube-system\nshop\n", ""
            if args == (
                "get",
                workload_triage.CONTROLLERS_RESOURCE,
                "--all-namespaces",
                *workload_triage.NAMESPACE_PROJECTION_ARGS,
            ):
                return 0, "shop\n", ""
            if args == (
                "get",
                "events",
                "--all-namespaces",
                *workload_triage.WARNING_EVENTS_FIELD_SELECTOR,
                *workload_triage.NAMESPACE_PROJECTION_ARGS,
            ):
                return 0, "shop\n", ""
            if args == ("get", "pods", "-n", "shop", "-o", "json"):
                return 0, json.dumps({"items": [shop_pod]}), ""
            if args == ("get", "pods", "-n", "kube-system", "-o", "json"):
                return 0, json.dumps({"items": [sys_pod]}), ""
            if args == ("get", workload_triage.CONTROLLERS_RESOURCE, "-n", "shop", "-o", "json"):
                return 0, json.dumps({"items": [short_dep]}), ""
            if args == ("get", "events", "-n", "shop", "--field-selector", "type=Warning", "-o", "json"):
                return 0, json.dumps({"items": [warn_ev]}), ""
            raise AssertionError(f"unexpected kubectl call {args}")

        code, out, err = self.run_main([], run)
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        self.assertEqual(calls.count(workload_triage.NAMESPACES_ARGS), 0)
        self.assertIn("pod-crashloop-oom", out)
        self.assertIn("Deployment/api", out)
        self.assertIn("warning events (60m): 3", out)
        self.assertTrue(out.rstrip().endswith("unhealthy: 2\nsystem unhealthy: 0"))

    def test_truncated_namespace_projection_falls_back_to_cached_namespace_list(self):
        cut = '{"apiVersion":"v1","kind":"List","items":[{"metadata":{"name":"cut'
        shop_pod = pod("api", namespace="shop")
        calls = []

        def run(args):
            calls.append(args)
            if args in (workload_triage.PODS_ARGS, workload_triage.CONTROLLERS_ARGS):
                return 0, cut, "credential proxy output truncated\n"
            if args in (
                ("get", "pods", "--all-namespaces", *workload_triage.NAMESPACE_PROJECTION_ARGS),
                (
                    "get",
                    workload_triage.CONTROLLERS_RESOURCE,
                    "--all-namespaces",
                    *workload_triage.NAMESPACE_PROJECTION_ARGS,
                ),
            ):
                return 0, "shop\n", "credential proxy output truncated\n"
            if args == workload_triage.NAMESPACES_ARGS:
                return 0, "namespace/shop\n", ""
            if args == ("get", "pods", "-n", "shop", "-o", "json"):
                return 0, json.dumps({"items": [shop_pod]}), ""
            if args == ("get", workload_triage.CONTROLLERS_RESOURCE, "-n", "shop", "-o", "json"):
                return 0, json.dumps({"items": []}), ""
            if args == workload_triage.EVENTS_ARGS:
                return 0, json.dumps({"items": []}), ""
            raise AssertionError(f"unexpected kubectl call {args}")

        code, out, err = self.run_main([], run)
        self.assertEqual(code, 0, err)
        self.assertEqual(calls.count(workload_triage.NAMESPACES_ARGS), 1)
        self.assertTrue(out.rstrip().endswith("unhealthy: 0\nsystem unhealthy: 0"))

    def test_truncated_single_namespace_falls_back_to_batched_names_with_ignore_not_found(self):
        cut = '{"apiVersion":"v1","kind":"List","items":[{"metadata":{"name":"cut'
        p1 = pod("p1", namespace="batch")
        p2 = pod("p2", namespace="batch", ready=False, waiting="ImagePullBackOff")

        def run(args):
            if args == workload_triage.PODS_ARGS:
                return 0, cut, "credential proxy output truncated\n"
            if args == ("get", "pods", "--all-namespaces", *workload_triage.NAMESPACE_PROJECTION_ARGS):
                return 0, "batch\n", ""
            if args == ("get", "pods", "-n", "batch", "-o", "json"):
                return 0, cut, "credential proxy output truncated\n"
            if args == ("get", "pods", "-n", "batch", "-o", "name"):
                return 0, "pod/p1\npod/p2\n", ""
            if args == ("get", "--ignore-not-found", "pod/p1", "pod/p2", "-n", "batch", "-o", "json"):
                return 0, json.dumps({"items": [p1, p2]}), ""
            if args in (workload_triage.CONTROLLERS_ARGS, workload_triage.EVENTS_ARGS):
                return 0, json.dumps({"items": []}), ""
            raise AssertionError(f"unexpected kubectl call {args}")

        code, out, err = self.run_main([], run)
        self.assertEqual(code, 0, err)
        self.assertIn("pod-container-creating", out)
        self.assertTrue(out.rstrip().endswith("unhealthy: 1\nsystem unhealthy: 0"))

    def test_truncated_object_batch_halves_until_chunks_fit_and_fails_closed_at_one(self):
        cut = '{"apiVersion":"v1","kind":"List","items":[{"metadata":{"name":"cut'
        pods_by_name = {
            f"pod/p{i}": pod(
                f"p{i}",
                namespace="batch",
                ready=(i != 3),
                waiting=("CrashLoopBackOff" if i == 3 else None),
            )
            for i in range(1, 5)
        }
        batch_calls: list[tuple[str, ...]] = []

        def run(args):
            if args == workload_triage.PODS_ARGS:
                return 0, cut, "credential proxy output truncated\n"
            if args == ("get", "pods", "--all-namespaces", *workload_triage.NAMESPACE_PROJECTION_ARGS):
                return 0, "batch\n", ""
            if args == ("get", "pods", "-n", "batch", "-o", "json"):
                return 0, cut, "credential proxy output truncated\n"
            if args == ("get", "pods", "-n", "batch", "-o", "name"):
                return 0, "\n".join(pods_by_name) + "\n", ""
            if args[:2] == ("get", "--ignore-not-found") and args[-4:] == ("-n", "batch", "-o", "json"):
                names = args[2:-4]
                batch_calls.append(names)
                if len(names) > 1:
                    return 0, cut, "credential proxy output truncated\n"
                return 0, json.dumps({"items": [pods_by_name[names[0]]]}), ""
            if args in (workload_triage.CONTROLLERS_ARGS, workload_triage.EVENTS_ARGS):
                return 0, json.dumps({"items": []}), ""
            raise AssertionError(f"unexpected kubectl call {args}")

        code, out, err = self.run_main([], run)
        self.assertEqual(code, 0, err)
        self.assertEqual(
            batch_calls,
            [
                ("pod/p1", "pod/p2", "pod/p3", "pod/p4"),
                ("pod/p1", "pod/p2"),
                ("pod/p1",),
                ("pod/p2",),
                ("pod/p3", "pod/p4"),
                ("pod/p3",),
                ("pod/p4",),
            ],
        )
        self.assertIn("pod-crashloop-oom", out)
        self.assertTrue(out.rstrip().endswith("unhealthy: 1\nsystem unhealthy: 0"))

        def single_object_over_cap(args):
            if args == workload_triage.PODS_ARGS:
                return 0, cut, "credential proxy output truncated\n"
            if args == ("get", "pods", "--all-namespaces", *workload_triage.NAMESPACE_PROJECTION_ARGS):
                return 0, "batch\n", ""
            if args == ("get", "pods", "-n", "batch", "-o", "json"):
                return 0, cut, "credential proxy output truncated\n"
            if args == ("get", "pods", "-n", "batch", "-o", "name"):
                return 0, "pod/p1\npod/p2\n", ""
            if args[:2] == ("get", "--ignore-not-found") and args[-4:] == ("-n", "batch", "-o", "json"):
                return 0, cut, "credential proxy output truncated\n"
            raise AssertionError(f"unexpected kubectl call {args}")

        f_code, f_out, f_err = self.run_main([], single_object_over_cap)
        self.assertEqual(f_code, 2)
        self.assertEqual(f_out, "")
        self.assertIn("cannot read pods in batch: credential proxy output truncated", f_err)

    def test_batched_fallback_tolerates_objects_deleted_between_list_and_get(self):
        cut = '{"apiVersion":"v1","kind":"List","items":[{"metadata":{"name":"cut'
        p2 = pod("p2", namespace="batch", ready=False, waiting="ImagePullBackOff")

        def run(args):
            if args in (workload_triage.PODS_ARGS, workload_triage.EVENTS_ARGS):
                return 0, cut, "credential proxy output truncated\n"
            if args == ("get", "pods", "--all-namespaces", *workload_triage.NAMESPACE_PROJECTION_ARGS):
                return 0, "batch\n", ""
            if args == (
                "get",
                "events",
                "--all-namespaces",
                *workload_triage.WARNING_EVENTS_FIELD_SELECTOR,
                *workload_triage.NAMESPACE_PROJECTION_ARGS,
            ):
                return 0, "batch\n", ""
            if args == ("get", "pods", "-n", "batch", "-o", "json"):
                return 0, cut, "credential proxy output truncated\n"
            if args == ("get", "pods", "-n", "batch", "-o", "name"):
                return 0, "pod/vanished\npod/p2\n", ""
            if args == ("get", "--ignore-not-found", "pod/vanished", "pod/p2", "-n", "batch", "-o", "json"):
                return 0, json.dumps({"items": [p2]}), ""
            if args == ("get", "events", "-n", "batch", "--field-selector", "type=Warning", "-o", "json"):
                return 0, cut, "credential proxy output truncated\n"
            if args == ("get", "events", "-n", "batch", "--field-selector", "type=Warning", "-o", "name"):
                return 0, "event/expired-1\nevent/expired-2\n", ""
            # When every named object in a batch vanished, `kubectl get --ignore-not-found -o json`
            # exits 0 with empty stdout.
            if args == ("get", "--ignore-not-found", "event/expired-1", "event/expired-2", "-n", "batch", "-o", "json"):
                return 0, "", ""
            if args == workload_triage.CONTROLLERS_ARGS:
                return 0, json.dumps({"items": []}), ""
            raise AssertionError(f"unexpected kubectl call {args}")

        code, out, err = self.run_main([], run)
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        self.assertIn("pod-container-creating", out)
        self.assertIn("warning events (60m): 0", out)
        self.assertTrue(out.rstrip().endswith("unhealthy: 1\nsystem unhealthy: 0"))

    def test_per_namespace_fallback_runs_concurrently_and_exits_2_when_budget_expires(self):
        cut = '{"apiVersion":"v1","kind":"List","items":[{"metadata":{"name":"cut'
        first_wave = threading.Barrier(workload_triage.FALLBACK_CONCURRENCY, timeout=5.0)
        namespaces = [f"ns-{i}" for i in range(workload_triage.FALLBACK_CONCURRENCY * 2)]
        flight_lock = threading.Lock()
        in_flight = [0]
        peak_in_flight = [0]
        started = [0]

        def concurrent_run(args):
            if args == workload_triage.PODS_ARGS:
                return 0, cut, "credential proxy output truncated\n"
            if args == ("get", "pods", "--all-namespaces", *workload_triage.NAMESPACE_PROJECTION_ARGS):
                return 0, "\n".join(namespaces) + "\n", ""
            if len(args) == 6 and args[:3] == ("get", "pods", "-n") and args[4:] == ("-o", "json"):
                with flight_lock:
                    in_flight[0] += 1
                    peak_in_flight[0] = max(peak_in_flight[0], in_flight[0])
                    slot = started[0]
                    started[0] += 1
                try:
                    if slot < workload_triage.FALLBACK_CONCURRENCY:
                        first_wave.wait()
                        time.sleep(0.02)
                    ns = args[3]
                    return 0, json.dumps({"items": [pod(f"p-{ns}", namespace=ns, ready=False, waiting="CrashLoopBackOff")]}), ""
                finally:
                    with flight_lock:
                        in_flight[0] -= 1
            if args in (workload_triage.CONTROLLERS_ARGS, workload_triage.EVENTS_ARGS):
                return 0, json.dumps({"items": []}), ""
            raise AssertionError(f"unexpected kubectl call {args}")

        code, out, err = self.run_main(["--json"], concurrent_run)
        self.assertEqual(code, 0, err)
        parsed = json.loads(out)
        self.assertEqual([r["namespace"] for r in parsed["unhealthy_pods"]], namespaces)
        self.assertEqual(peak_in_flight[0], workload_triage.FALLBACK_CONCURRENCY)

        elapsed = [0.0]
        lock = threading.Lock()
        ns_pod_calls: list[str] = []

        def fake_clock():
            with lock:
                return elapsed[0]

        def slow_run(args):
            if args == workload_triage.PODS_ARGS:
                return 0, cut, "credential proxy output truncated\n"
            if args == ("get", "pods", "--all-namespaces", *workload_triage.NAMESPACE_PROJECTION_ARGS):
                return 0, "\n".join(namespaces) + "\n", ""
            if len(args) == 6 and args[:3] == ("get", "pods", "-n"):
                with lock:
                    ns_pod_calls.append(args[3])
                    elapsed[0] += 5.0
                return 0, json.dumps({"items": [pod("web", namespace=args[3])]}), ""
            raise AssertionError(f"controllers/events must not be reached after pod fallback budget overrun: {args}")

        b_code, b_out, b_err = self.run_main([], slow_run, clock=fake_clock, budget_seconds=5.0)
        self.assertEqual(b_code, 2)
        self.assertEqual(b_out, "")
        self.assertRegex(b_err, r"cannot read pods in ns-\d+: workload triage scan budget exceeded")
        self.assertLessEqual(len(ns_pod_calls), workload_triage.FALLBACK_CONCURRENCY)
        self.assertLess(len(ns_pod_calls), len(namespaces))

    def test_per_namespace_fallback_aborts_queued_reads_on_fast_failure_and_preserves_order(self):
        cut = '{"items": ['
        namespaces = [f"ns-{i}" for i in range(workload_triage.FALLBACK_CONCURRENCY + 20)]
        ns2_names = [f"pod/p-ns-2-{i}" for i in range(workload_triage.OBJECT_BATCH_SIZE * 3)]
        real_event = threading.Event
        ns2_in_batch1 = real_event()
        cancel_observed = real_event()
        dispatch_lock = threading.Lock()
        dispatched: list[str] = []
        ns2_batches: list[tuple[str, ...]] = []

        class ObservableCancelEvent:
            def __init__(self) -> None:
                self._inner = real_event()

            def is_set(self) -> bool:
                return self._inner.is_set()

            def set(self) -> None:
                self._inner.set()
                cancel_observed.set()

        def fast_fail_run(args):
            if args == workload_triage.PODS_ARGS:
                return 0, cut, "credential proxy output truncated\n"
            if args == ("get", "pods", "--all-namespaces", *workload_triage.NAMESPACE_PROJECTION_ARGS):
                return 0, "\n".join(namespaces) + "\n", ""
            if len(args) == 6 and args[:3] == ("get", "pods", "-n") and args[4:] == ("-o", "json"):
                ns = args[3]
                with dispatch_lock:
                    dispatched.append(ns)
                if ns == "ns-2":
                    return 0, cut, "credential proxy output truncated\n"
                if ns == "ns-1":
                    self.assertTrue(ns2_in_batch1.wait(timeout=2.0))
                    return 1, "", "Error from server (Forbidden): pods is forbidden"
                self.assertTrue(cancel_observed.wait(timeout=2.0))
                return 0, json.dumps({"items": [pod(f"p-{ns}", namespace=ns)]}), ""
            if args == ("get", "pods", "-n", "ns-2", "-o", "name"):
                return 0, "\n".join(ns2_names) + "\n", ""
            if (
                len(args) > 6
                and args[:2] == ("get", "--ignore-not-found")
                and args[-4:] == ("-n", "ns-2", "-o", "json")
            ):
                with dispatch_lock:
                    ns2_batches.append(args)
                ns2_in_batch1.set()
                self.assertTrue(cancel_observed.wait(timeout=2.0))
                return 0, json.dumps({"items": [pod("p-ns-2-0", namespace="ns-2")]}), ""
            raise AssertionError(f"unexpected kubectl call after fast ns-1 failure: {args}")

        with patch.object(
            workload_triage,
            "threading",
            types.SimpleNamespace(Event=ObservableCancelEvent, Lock=threading.Lock),
        ):
            code, out, err = self.run_main([], fast_fail_run)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("cannot read pods in ns-1: Error from server (Forbidden)", err)
        self.assertLessEqual(len(dispatched), workload_triage.FALLBACK_CONCURRENCY)
        self.assertTrue(set(dispatched).issubset({f"ns-{i}" for i in range(workload_triage.FALLBACK_CONCURRENCY)}))
        self.assertEqual(len(ns2_batches), 1)

        order_namespaces = [f"ns-{i}" for i in range(workload_triage.FALLBACK_CONCURRENCY)]
        later_finished = threading.Event()
        done_lock = threading.Lock()
        later_count = [0]

        def out_of_order_run(args):
            if args == workload_triage.PODS_ARGS:
                return 0, cut, "credential proxy output truncated\n"
            if args == ("get", "pods", "--all-namespaces", *workload_triage.NAMESPACE_PROJECTION_ARGS):
                return 0, "\n".join(order_namespaces) + "\n", ""
            if len(args) == 6 and args[:3] == ("get", "pods", "-n") and args[4:] == ("-o", "json"):
                ns = args[3]
                payload = json.dumps(
                    {"items": [pod(f"p-{ns}", namespace=ns, ready=False, waiting="CrashLoopBackOff")]}
                )
                if ns == "ns-0":
                    later_finished.wait(timeout=2.0)
                    time.sleep(0.02)
                    return 0, payload, ""
                with done_lock:
                    later_count[0] += 1
                    if later_count[0] == len(order_namespaces) - 1:
                        later_finished.set()
                return 0, payload, ""
            if args in (workload_triage.CONTROLLERS_ARGS, workload_triage.EVENTS_ARGS):
                return 0, json.dumps({"items": []}), ""
            raise AssertionError(f"unexpected kubectl call {args}")

        o_code, o_out, o_err = self.run_main(["--json"], out_of_order_run)
        self.assertEqual(o_code, 0, o_err)
        self.assertEqual(
            [r["namespace"] for r in json.loads(o_out)["unhealthy_pods"]],
            order_namespaces,
        )

    def test_bounded_runner_passes_remaining_budget_timeout_to_subprocess_run(self):
        with patch.object(workload_triage.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["kubectl", "get", "pods"],
                returncode=0,
                stdout='{"items":[]}',
                stderr="",
            )
            near_deadline = workload_triage._bounded_runner(
                workload_triage.run_kubectl,
                lambda: 92.5,
                100.0,
            )
            code, out, err = near_deadline(("get", "pods"))
            self.assertEqual(code, 0)
            self.assertEqual(out, '{"items":[]}')
            self.assertEqual(err, "")
            self.assertAlmostEqual(mock_run.call_args.kwargs["timeout"], 7.5)

            far_deadline = workload_triage._bounded_runner(
                workload_triage.run_kubectl,
                lambda: 0.0,
                200.0,
            )
            far_deadline(("get", "pods"))
            self.assertAlmostEqual(
                mock_run.call_args.kwargs["timeout"],
                float(workload_triage.KUBECTL_CAP_SECONDS),
            )

            mock_run.side_effect = subprocess.TimeoutExpired(cmd=["kubectl", "get", "pods"], timeout=7.5)
            t_code, t_out, t_err = near_deadline(("get", "pods"))
            self.assertEqual(t_code, workload_triage.EXIT_TIMED_OUT)
            self.assertEqual(t_out, "")
            self.assertIn("timed out", t_err.lower())

    def test_unrecoverable_truncation_on_controllers_or_events_exits_2_not_clean_zero(self):
        for target_args, proj_args, label in (
            (
                workload_triage.CONTROLLERS_ARGS,
                (
                    "get",
                    workload_triage.CONTROLLERS_RESOURCE,
                    "--all-namespaces",
                    *workload_triage.NAMESPACE_PROJECTION_ARGS,
                ),
                "deployments, statefulsets, daemonsets and jobs",
            ),
            (
                workload_triage.EVENTS_ARGS,
                (
                    "get",
                    "events",
                    "--all-namespaces",
                    *workload_triage.WARNING_EVENTS_FIELD_SELECTOR,
                    *workload_triage.NAMESPACE_PROJECTION_ARGS,
                ),
                "warning events",
            ),
        ):
            with self.subTest(section=label):
                def run(args, bad=target_args, proj=proj_args):
                    if args == bad:
                        return 0, '{"items":[]}', "credential proxy output truncated\n"
                    if args == proj:
                        return 1, "", "connection refused"
                    return 0, json.dumps({"items": [pod("web")] if args == workload_triage.PODS_ARGS else []}), ""

                code, out, err = self.run_main([], run)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn(f"cannot read {label}", err)

    def test_markerless_json_parse_error_recovers_via_fallback_or_exits_2(self):
        pods_proj = (
            "get",
            "pods",
            "--all-namespaces",
            *workload_triage.NAMESPACE_PROJECTION_ARGS,
        )
        for bad_stdout in ('{"items":[{"metadata":', "[]"):
            with self.subTest(bad_stdout=bad_stdout, fallback="refused"):
                def run_refused(args, payload=bad_stdout):
                    if args == workload_triage.PODS_ARGS:
                        return 0, payload, ""
                    if args == pods_proj:
                        return 1, "", "connection refused"
                    return 0, '{"items":[]}', ""

                code, out, err = self.run_main([], run_refused)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn(
                    "cannot read pods: cannot parse output as JSON (namespace fallback failed: connection refused)",
                    err,
                )

            with self.subTest(bad_stdout=bad_stdout, fallback="recovered"):
                def run_recovered(args, payload=bad_stdout):
                    if args == workload_triage.PODS_ARGS:
                        return 0, payload, ""
                    if args == pods_proj:
                        return 0, "default\n", ""
                    if args == ("get", "pods", "-n", "default", "-o", "json"):
                        return 0, json.dumps({"items": [pod("web")]}), ""
                    return 0, '{"items":[]}', ""

                code, out, err = self.run_main([], run_recovered)
                self.assertEqual(code, 0, err)
                self.assertIn("pods: 1 (healthy 1, initializing 0, unhealthy 0)", out)
                self.assertTrue(out.rstrip().endswith("unhealthy: 0\nsystem unhealthy: 0"))


class SkillNamesTest(unittest.TestCase):
    def test_every_handoff_names_a_shipped_cluster_skill(self):
        """The SKILL column is read by the agent as a skill to load, so each value must exist."""
        skills_dir = os.path.join(os.path.dirname(__file__), "..", "..", "cluster", "skills")
        names = [v for k, v in vars(workload_triage).items() if k.startswith("SKILL_")]
        self.assertGreaterEqual(len(names), 3)
        for name in names:
            self.assertTrue(os.path.isfile(os.path.join(skills_dir, name, "SKILL.md")), name)

    def test_table_renders_headers_alone(self):
        self.assertEqual(workload_triage._table(["A", "BB"], []), ["A  BB"])

    def test_broker_and_terminal_timing_constants_match_sources(self):
        here = Path(__file__).resolve().parent
        broker = (here / "credential_proxy.py").read_text(encoding="utf-8")
        preflight = (here / "cluster_preflight.sh").read_text(encoding="utf-8")
        sop = (here.parent / "governance" / "obtainability_audit_sop.md").read_text(encoding="utf-8")

        def declared(pattern: str, text: str) -> int:
            match = re.search(pattern, text, re.MULTILINE)
            self.assertIsNotNone(match, pattern)
            return int(match.group(1))

        self.assertEqual(
            workload_triage.TERMINAL_TOOL_TIMEOUT_SECONDS,
            declared(r"the default (\d+)-second timeout", sop),
        )
        self.assertEqual(
            workload_triage.TERMINAL_TOOL_TIMEOUT_SECONDS,
            declared(r"^readonly TERMINAL_TOOL_TIMEOUT_SECONDS=(\d+)$", preflight),
        )
        self.assertEqual(
            workload_triage.BROKER_ADMISSION_WAIT_SECONDS,
            declared(r"^COMMAND_SLOT_WAIT_SECONDS = (\d+)$", broker),
        )
        self.assertEqual(
            workload_triage.BROKER_KUBECTL_RUN_SECONDS,
            declared(r"^DEFAULT_KUBECTL_TIMEOUT_SECONDS = (\d+)$", broker),
        )
        self.assertEqual(
            workload_triage.KUBECTL_CAP_SECONDS,
            workload_triage.BROKER_ADMISSION_WAIT_SECONDS
            + workload_triage.BROKER_KUBECTL_RUN_SECONDS
            + workload_triage.KUBECTL_CAP_MARGIN_SECONDS,
        )


class MainTest(unittest.TestCase):
    def run_main(self, argv, run, context="test-context"):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = workload_triage.main(argv, run=run, now=NOW, context=context)
        return code, out.getvalue(), err.getvalue()

    def test_healthy_cluster_ends_with_zero_and_three_api_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            kc = Path(tmp) / "kubeconfig.yaml"
            kc.write_text("apiVersion: v1\ncurrent-context: test-context\n", encoding="utf-8")
            run = stub(pods=[pod("web")], controllers=[deploy("web")])
            with patch.dict(os.environ, {"KUBECONFIG": str(kc)}, clear=False):
                code, out, _ = self.run_main([], run, context=None)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "scanned 2026-09-09T12:00:00Z context test-context")
        self.assertTrue(out.rstrip().endswith("unhealthy: 0\nsystem unhealthy: 0"))
        self.assertEqual(len(run.calls), 3)
        self.assertTrue(all(a[0] == "get" for a in run.calls), run.calls)

    def test_text_output_names_the_skill(self):
        run = stub(pods=[pod("api", ready=False, waiting="CrashLoopBackOff")])
        code, out, _ = self.run_main([], run)
        self.assertEqual(code, 0)
        self.assertIn("pod-crashloop-oom", out)
        self.assertTrue(out.rstrip().endswith("unhealthy: 1\nsystem unhealthy: 0"))

    def test_history_sections_and_summary_suffix_render(self):
        old_term = {"exitCode": 137, "reason": "OOMKilled", "finishedAt": stamp(300)}
        evicted = pod("evicted", phase="Failed", ready=False, age=600)
        evicted["status"]["reason"] = "Evicted"
        run = stub(pods=[pod("web", last_term=old_term, restarts=3), evicted])
        code, out, _ = self.run_main([], run)
        self.assertEqual(code, 0)
        self.assertIn("failed earlier 1)", out.splitlines()[1])
        self.assertIn("RESTARTED EARLIER", out)
        self.assertIn("137 OOMKilled", out)
        self.assertIn("FAILED EARLIER", out)
        self.assertTrue(out.rstrip().endswith("unhealthy: 0\nsystem unhealthy: 0"))

    def test_json_output(self):
        code, out, _ = self.run_main(["--json"], stub(pods=[pod("web")]))
        self.assertEqual(code, 0)
        summary = json.loads(out)["summary"]
        self.assertEqual(summary["unhealthy"], 0)
        self.assertEqual(summary["system_unhealthy"], 0)

    def test_failed_pod_read_exits_2(self):
        code, out, err = self.run_main([], stub(fail="pods"))
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("cannot read pods", err)

    def test_failed_controller_read_exits_2(self):
        code, out, err = self.run_main([], stub(pods=[pod("web")], fail=workload_triage.CONTROLLERS_RESOURCE))
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("cannot read deployments, statefulsets, daemonsets and jobs", err)

    def test_failed_event_read_exits_2(self):
        code, out, err = self.run_main([], stub(pods=[pod("web")], fail="events"))
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("cannot read warning events", err)

    def test_abbreviated_flags_are_refused(self):
        with self.assertRaises(SystemExit):
            self.run_main(["--include"], stub())

    def test_render_text_prints_all_rows_and_parser_omits_rerun_knobs(self):
        events = [event(f"pod-{i}", "BackOff", 2) for i in range(30)]
        code, out, _ = self.run_main([], stub(pods=[pod("web")], events=events))
        self.assertEqual(code, 0)
        for i in range(30):
            self.assertIn(f"Pod/pod-{i}", out)
        self.assertNotIn("--max-rows", out)
        self.assertNotIn("--json for all", out)
        for removed in ("--max-rows", "--events-minutes", "--init-grace-minutes"):
            with self.assertRaises(SystemExit):
                self.run_main([removed, "10"], stub())


if __name__ == "__main__":
    unittest.main()
