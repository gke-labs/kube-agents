#!/usr/bin/env python3
"""Unit tests for workload_triage.py: fixture objects, kubectl stubbed."""

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone

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
        if args == workload_triage.CONTEXT_ARGS:
            return 0, "test-context\n", ""
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
    return workload_triage.triage(stub(**kw), NOW, include, 10, 60)


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
            ]
        )
        self.assertEqual(
            [r["controller"] for r in report["controllers_below_desired"]],
            ["Deployment/short", "DaemonSet/ds"],
        )
        self.assertEqual(report["summary"]["unhealthy"], 2)


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

    def test_node_events_are_folded_into_one_summary(self):
        report = triage(events=[event("n1", "KubeletStart", 5, namespace="default", kind="Node")])
        self.assertEqual(report["warning_events"], [])
        self.assertEqual(report["node_warning_events"]["count"], 1)


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
        self.assertEqual(report["summary"]["pods"], 1)
        self.assertEqual(report["system_namespaces"]["kube-system"]["unhealthy"], 1)
        self.assertIn("gmp-system", report["system_namespaces_healthy"])

    def test_system_restarts_ride_on_the_summary_line(self):
        old = {"exitCode": 1, "reason": "Error", "finishedAt": stamp(300)}
        run = stub(pods=[pod("netd-x", namespace="kube-system", last_term=old, restarts=2), pod("web")])
        out = io.StringIO()
        with redirect_stdout(out):
            workload_triage.main([], run=run, now=NOW)
        text = out.getvalue()
        self.assertIn("kube-system: 0/1 pods unhealthy", text)
        self.assertIn("restarted pods: netd-x 2x, last 5h ago", text)
        self.assertTrue(text.rstrip().endswith("unhealthy: 0"))

    def test_system_restart_in_window_is_named_first_despite_the_cap(self):
        boot = {"exitCode": 255, "reason": "Unknown", "finishedAt": stamp(3000)}
        pods = [pod(f"sys-{i}", namespace="kube-system", last_term=boot, restarts=9) for i in range(7)]
        recent = {"exitCode": 1, "reason": "Error", "finishedAt": stamp(10)}
        pods.append(pod("netd-new", namespace="kube-system", last_term=recent, restarts=1))
        out = io.StringIO()
        with redirect_stdout(out):
            workload_triage.main([], run=stub(pods=pods), now=NOW)
        line = [ln for ln in out.getvalue().splitlines() if ln.startswith("kube-system:")][0]
        self.assertIn("1/8 pods unhealthy (RecentRestart 1)", line)
        self.assertIn("restarted pods: netd-new 1x, last 10m ago (in window), sys-", line)
        self.assertIn("(+3 more; --include-system lists them)", line)

    def test_include_system_lists_them(self):
        report = triage(
            pods=[pod("netd", namespace="kube-system", ready=False, waiting="CrashLoopBackOff")],
            include_system=True,
        )
        self.assertEqual(report["summary"]["unhealthy"], 1)

    def test_namespace_rules(self):
        for ns in ("kube-system", "gke-managed-cim", "gmp-public", "cnrm-system"):
            self.assertTrue(workload_triage.is_system_namespace(ns), ns)
        for ns in ("default", "prod", "systemd-tools"):
            self.assertFalse(workload_triage.is_system_namespace(ns), ns)


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


class MainTest(unittest.TestCase):
    def run_main(self, argv, run):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = workload_triage.main(argv, run=run, now=NOW)
        return code, out.getvalue(), err.getvalue()

    def test_healthy_cluster_ends_with_zero_and_three_api_reads(self):
        run = stub(pods=[pod("web")], controllers=[deploy("web")])
        code, out, _ = self.run_main([], run)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "scanned 2026-09-09T12:00:00Z context test-context")
        self.assertTrue(out.rstrip().endswith("unhealthy: 0"))
        api_reads = [a for a in run.calls if a[0] == "get"]
        self.assertEqual(len(api_reads), 3)
        self.assertEqual([a for a in run.calls if a[0] != "get"], [workload_triage.CONTEXT_ARGS])

    def test_text_output_names_the_skill(self):
        run = stub(pods=[pod("api", ready=False, waiting="CrashLoopBackOff")])
        code, out, _ = self.run_main([], run)
        self.assertEqual(code, 0)
        self.assertIn("pod-crashloop-oom", out)
        self.assertTrue(out.rstrip().endswith("unhealthy: 1"))

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
        self.assertTrue(out.rstrip().endswith("unhealthy: 0"))

    def test_json_output(self):
        code, out, _ = self.run_main(["--json"], stub(pods=[pod("web")]))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["summary"]["unhealthy"], 0)

    def test_failed_pod_read_exits_2(self):
        code, out, err = self.run_main([], stub(fail="pods"))
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("cannot read pods", err)

    def test_failed_event_read_still_reports(self):
        code, out, err = self.run_main([], stub(pods=[pod("web")], fail="events"))
        self.assertEqual(code, 0)
        self.assertIn("cannot read warning events", err)
        self.assertIn("unhealthy: 0", out)

    def test_abbreviated_flags_are_refused(self):
        with self.assertRaises(SystemExit):
            self.run_main(["--include"], stub())


if __name__ == "__main__":
    unittest.main()
