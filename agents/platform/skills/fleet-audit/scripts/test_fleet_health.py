#!/usr/bin/env python3
"""Tests for fleet_health.py, the fleet health sweep."""

import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import fleet_health as fh  # noqa: E402

# fleet_drift's suite has the reasoning: the host's scope guard must not turn
# these listing tests into refusals.
os.environ.pop("KUBEAGENTS_SCOPE_DECLARED", None)
import fleet_scope_args as _fleet_scope_args  # noqa: E402

_fleet_scope_args.SCOPE_DECLARED_FILE = "/nonexistent/kube-agents-sandbox/scope-declared"


def run_of(rc, stdout="", stderr=""):
    return fh.Run(["x"], rc, stdout, stderr, 0.01)


def cluster(name, location="us-central1", status="RUNNING", pools=None, packages=("SYSTEM_COMPONENTS", "POD", "DEPLOYMENT", "STATEFULSET"), **extra):
    doc = {
        "name": name,
        "location": location,
        "status": status,
        "nodePools": pools if pools is not None else [{"name": "default-pool", "status": "RUNNING"}],
        "monitoringConfig": {"componentConfig": {"enableComponents": list(packages)}},
    }
    doc.update(extra)
    return doc


def lister(clusters_by_project, *, projects=None):
    """A `run` that answers discovery and one `clusters list` per project."""
    projects = projects or list(clusters_by_project)

    def run(argv, **kwargs):
        if argv[:4] == ["gcloud", "config", "get-value", "project"]:
            return run_of(0, projects[0])
        if argv[:3] == ["gcloud", "projects", "list"]:
            return run_of(0, "\n".join(projects))
        if argv[:4] == ["gcloud", "container", "clusters", "list"]:
            project = argv[argv.index("--project") + 1]
            value = clusters_by_project[project]
            if isinstance(value, fh.Run):
                return value
            return run_of(0, json.dumps(value))
        raise AssertionError(f"unexpected command: {argv}")

    return run


def series(cluster_name, location="us-central1", ksm=False, value="1", **labels):
    key = "cluster" if ksm else "cluster_name"
    return {"metric": {key: cluster_name, "location": location, **labels}, "value": [0, value]}


def querier(hits=None, present=None, errors=None):
    """A `query` keyed on the metric name in the PromQL: `present` lists the
    clusters reporting each metric (default: every name in `hits`, plus `all`)."""
    hits = hits or {}
    errors = errors or {}
    calls = []

    def query(project, promql):
        calls.append((project, promql))
        for metric in ("node_status_condition", "container_restart_count", "kube_pod_status_phase", "kube_deployment", "kube_statefulset"):
            if metric in promql:
                break
        if metric in errors:
            return [], errors[metric]
        if not any(cmp in promql for cmp in ("== 1", ">=", "> 0")):
            ksm = metric in ("kube_pod_status_phase", "kube_deployment", "kube_statefulset")
            return [series(c, loc, ksm=ksm) for c, loc in (present or {}).get(metric, [])], None
        return hits.get(metric, []), None

    query.calls = calls
    return query


ALL_PRESENT = {
    "node_status_condition": [("a", "us-central1")],
    "container_restart_count": [("a", "us-central1")],
    "kube_pod_status_phase": [("a", "us-central1")],
    "kube_deployment": [("a", "us-central1")],
    "kube_statefulset": [("a", "us-central1")],
}


def checks(entry):
    return [(c["check"], c["severity"], c["object"]) for c in entry["candidates"]]


class ControlPlaneTest(unittest.TestCase):
    def test_a_healthy_cluster_has_no_candidate(self):
        self.assertEqual(fh.control_plane_candidates(cluster("a")), [])

    def test_cluster_status_severity(self):
        self.assertEqual(checks({"candidates": fh.control_plane_candidates(cluster("a", status="ERROR"))}),
                         [("cluster-not-running", "critical", "Cluster/a")])
        self.assertEqual(checks({"candidates": fh.control_plane_candidates(cluster("a", status="DEGRADED"))})[0][1],
                         "critical")
        self.assertEqual(checks({"candidates": fh.control_plane_candidates(cluster("a", status="RECONCILING"))})[0][1],
                         "minor")

    def test_the_status_message_is_in_the_excerpt(self):
        found = fh.control_plane_candidates(cluster("a", status="DEGRADED", statusMessage="nodes unreachable"))
        self.assertEqual(found[0]["excerpt"], "status DEGRADED: nodes unreachable")

    def test_conditions_and_pool_states(self):
        pools = [
            {"name": "p1", "status": "RUNNING_WITH_ERROR", "conditions": [{"code": "GCE_STOCKOUT", "message": "no capacity"}]},
            {"name": "p2", "status": "ERROR"},
            {"name": "p3", "status": "PROVISIONING"},
        ]
        found = fh.control_plane_candidates(cluster("a", pools=pools, conditions=[{"canonicalCode": "UNAVAILABLE", "message": "x"}]))
        self.assertEqual(checks({"candidates": found}), [
            ("cluster-condition", "major", "Cluster/a"),
            ("node-pool-not-running", "major", "NodePool/p1"),
            ("node-pool-condition", "major", "NodePool/p1"),
            ("node-pool-not-running", "critical", "NodePool/p2"),
            ("node-pool-not-running", "minor", "NodePool/p3"),
        ])
        self.assertEqual(found[2]["excerpt"], "GCE_STOCKOUT: no capacity")

    def test_a_gke_code_wins_over_the_canonical_code(self):
        both = [{"code": "GCE_QUOTA_EXCEEDED", "canonicalCode": "RESOURCE_EXHAUSTED", "message": "m"}]
        self.assertEqual(fh.control_plane_candidates(cluster("a", conditions=both))[0]["excerpt"], "GCE_QUOTA_EXCEEDED: m")
        unknown = [{"code": "UNKNOWN", "canonicalCode": "UNAVAILABLE", "message": "m"}]
        self.assertEqual(fh.control_plane_candidates(cluster("a", conditions=unknown))[0]["excerpt"], "UNAVAILABLE: m")


class MetricsTest(unittest.TestCase):
    def setUp(self):
        fh.declared_scope.set(None, None)

    def entry(self, query, c=None):
        return fh.collect_fleet(run=lister({"acme": [c or cluster("a")]}), query=query)["clusters"][0]

    def test_each_metric_hit_lands_on_its_cluster(self):
        query = querier(present=ALL_PRESENT, hits={
            "node_status_condition": [series("a", node_name="n1", status="Unknown")],
            "container_restart_count": [series("a", value="12.0", namespace_name="seeded-debug",
                                               pod_name="payments-api-1", container_name="api"),
                                        series("other", namespace_name="x", pod_name="y", container_name="z")],
            "kube_pod_status_phase": [series("a", ksm=True, namespace="shop", pod="web-1")],
        })
        entry = self.entry(query)
        self.assertEqual(checks(entry), [
            ("node-not-ready", "major", "Node/n1"),
            ("container-restarting", "major", "Pod/payments-api-1"),
            ("pod-pending", "major", "Pod/web-1"),
        ])
        restart = entry["candidates"][1]
        self.assertEqual(restart["namespace"], "seeded-debug")
        self.assertEqual(restart["excerpt"], "container api restarted 12 times in the last 1h")
        self.assertNotIn("checks_unevaluated", entry)

    def test_every_query_is_pinned_to_its_project(self):
        query = querier(present=ALL_PRESENT)
        self.entry(query)
        self.assertEqual(len(query.calls), 10)
        for project, promql in query.calls:
            self.assertEqual(project, "acme")
            self.assertIn('project_id="acme"', promql)

    def test_the_queries_drop_young_pending_pods_and_gone_containers(self):
        query = querier(present=ALL_PRESENT)
        self.entry(query)
        text = " ".join(q for _, q in query.calls)
        self.assertIn('offset 10m == 1', text)
        self.assertIn(") and on (location, cluster_name, namespace_name, pod_name, container_name)", text)
        for _, promql in query.calls:
            self.assertEqual(promql.count("{"), promql.count("}"))

    def test_a_deployment_short_of_ready_replicas_is_a_finding(self):
        query = querier(present=ALL_PRESENT, hits={
            "kube_deployment": [series("a", ksm=True, value="2", namespace="shop", deployment="checkout")]})
        entry = self.entry(query)
        self.assertEqual(checks(entry), [("deployment-unavailable", "major", "Deployment/checkout")])
        self.assertEqual(entry["candidates"][0]["namespace"], "shop")
        self.assertEqual(entry["candidates"][0]["excerpt"], "2 replica(s) short of available for at least 10m")
        promql = fh.DEPLOYMENT_QUERY.format(project="acme", exclude="")
        self.assertIn("[10m:1m]) > 0)", promql)
        self.assertIn("(kube_deployment_spec_replicas{project_id=\"acme\"} offset 10m))", promql)
        self.assertTrue(promql.endswith("(count by (location, cluster, namespace, deployment) "
                                        "(kube_deployment_status_replicas_available{project_id=\"acme\"}))"))

    def test_a_statefulset_short_of_ready_replicas_is_a_finding(self):
        query = querier(present=ALL_PRESENT, hits={
            "kube_statefulset": [series("a", ksm=True, value="1", namespace="db", statefulset="pg")]})
        entry = self.entry(query)
        self.assertEqual(checks(entry), [("statefulset-unready", "major", "StatefulSet/pg")])
        self.assertEqual(entry["candidates"][0]["excerpt"], "1 replica(s) short of Ready for at least 10m")

    def test_no_workloads_of_a_kind_on_a_live_cluster_is_evaluated(self):
        entry = self.entry(querier(present=ALL_PRESENT | {"kube_statefulset": [], "kube_deployment": []}))
        self.assertNotIn("checks_unevaluated", entry)

    def test_a_workload_metric_with_one_of_its_two_series_is_unevaluated(self):
        def query(project, promql):
            if "kube_deployment" in promql and not any(c in promql for c in ("> 0",)):
                return [series("a", ksm=True, value=fh.PRESENCE_ONE)], None
            return querier(present=ALL_PRESENT)(project, promql)

        entry = self.entry(query)
        self.assertEqual(entry["checks_unevaluated"],
                         [{"check": "deployment-unavailable", "reason": fh.METRIC_MISSING_REASON}])

    def test_the_workload_presence_read_tells_both_from_one(self):
        promql = fh.DEPLOYMENT_PRESENCE_QUERY.format(project="acme", exclude="")
        self.assertIn(") * 0 + " + fh.PRESENCE_BOTH + " or ", promql)
        self.assertEqual(promql.count("* 0 + " + fh.PRESENCE_ONE), 2)

    def test_presence_reads_the_findings_own_metric(self):
        self.assertIn('phase="Pending"', fh.PENDING_PRESENCE_QUERY)
        self.assertIn("kube_deployment_status_replicas_available", fh.DEPLOYMENT_PRESENCE_QUERY)
        self.assertIn("kube_statefulset_status_replicas_ready", fh.STATEFULSET_PRESENCE_QUERY)

    def test_the_pending_join_names_the_pod_labels(self):
        promql = fh.PENDING_QUERY.format(project="acme", exclude="")
        self.assertIn(") and on (location, cluster, namespace, pod) (count by (location, cluster, namespace, pod)", promql)
        self.assertTrue(promql.startswith("(count by (location, cluster, namespace, pod) (min_over_time("))

    def test_an_odd_restart_value_is_zero_not_a_crash(self):
        rows = [series("a", value=v, namespace_name="n", pod_name=f"p{i}", container_name="c")
                for i, v in enumerate(("NaN", "+Inf", "-Inf", "x", "4.6"))]
        entry = self.entry(querier(present=ALL_PRESENT, hits={"container_restart_count": rows}))
        self.assertEqual([c["excerpt"].split(" restarted ")[1].split(" ")[0] for c in entry["candidates"]],
                         ["0", "0", "0", "0", "5"])

    def test_the_metric_reads_are_recorded_once_per_project(self):
        manifest = fh.collect_fleet(run=lister({"acme": [cluster("a"), cluster("b")]}), query=querier(present=ALL_PRESENT))
        self.assertEqual(len(manifest["project_reads"]["acme"]), 10)
        for entry in manifest["clusters"]:
            self.assertEqual([c["check"] for c in entry["commands"]], [",".join(fh.CONTROL_PLANE_CHECKS)])

    def test_a_cluster_with_no_series_is_unevaluated_not_healthy(self):
        entry = self.entry(querier(present={"node_status_condition": [("a", "us-central1")]}))
        reasons = {u["check"]: u["reason"] for u in entry["checks_unevaluated"]}
        self.assertEqual(set(reasons), {"container-restarting", "pod-pending", "deployment-unavailable", "statefulset-unready"})
        self.assertIn("no series", reasons["container-restarting"])

    def test_a_metrics_package_that_is_off_is_named(self):
        entry = self.entry(querier(present=ALL_PRESENT | {"kube_pod_status_phase": []}),
                           cluster("a", packages=("SYSTEM_COMPONENTS", "DEPLOYMENT")))
        self.assertEqual(entry["checks_unevaluated"],
                         [{"check": "pod-pending", "reason": "the cluster's POD metrics package is off"}])

    def test_a_failed_read_is_unevaluated_with_its_error(self):
        entry = self.entry(querier(present=ALL_PRESENT, errors={"container_restart_count": "HTTP 403: denied"}))
        self.assertEqual(entry["checks_unevaluated"],
                         [{"check": "container-restarting", "reason": "metric read failed: HTTP 403: denied"}])

    def test_a_degraded_cluster_still_gets_its_metric_checks(self):
        query = querier(present=ALL_PRESENT, hits={"node_status_condition": [series("a", node_name="n1", status="False")]})
        entry = self.entry(query, cluster("a", status="DEGRADED"))
        self.assertEqual(checks(entry), [("cluster-not-running", "critical", "Cluster/a"),
                                         ("node-not-ready", "major", "Node/n1")])
        self.assertNotIn("checks_unevaluated", entry)

    def test_a_cluster_with_no_series_names_its_status(self):
        entry = self.entry(querier(), cluster("a", status="ERROR"))
        self.assertEqual({u["check"] for u in entry["checks_unevaluated"]}, set(fh.METRIC_CHECKS))
        self.assertIn("status ERROR", entry["checks_unevaluated"][0]["reason"])

    def test_clusters_with_one_name_in_two_locations_stay_apart(self):
        query = querier(present={"container_restart_count": [("a", "us-central1"), ("a", "europe-west1")]},
                        hits={"container_restart_count": [series("a", location="europe-west1", value="5",
                                                                 namespace_name="n", pod_name="p", container_name="c")]})
        manifest = fh.collect_fleet(run=lister({"acme": [cluster("a"), cluster("a", location="europe-west1")]}), query=query)
        by_name = {e["name"]: e for e in manifest["clusters"]}
        self.assertEqual(by_name["acme/us-central1/a"]["candidates"], [])
        self.assertEqual(len(by_name["acme/europe-west1/a"]["candidates"]), 1)

    def test_a_long_list_is_capped_and_counted(self):
        many = [series("a", value="9", namespace_name="n", pod_name=f"p{i}", container_name="c")
                for i in range(fh.MAX_METRIC_CANDIDATES_PER_CHECK + 3)]
        entry = self.entry(querier(present=ALL_PRESENT, hits={"container_restart_count": many}))
        self.assertEqual(len(entry["candidates"]), fh.MAX_METRIC_CANDIDATES_PER_CHECK)
        self.assertEqual(entry["limitations"], "container-restarting lists 20 of 23; the rest are not listed.")
        self.assertNotIn("checks_unevaluated", entry)


class FleetTest(unittest.TestCase):
    def setUp(self):
        fh.declared_scope.set(None, None)

    def tearDown(self):
        fh.declared_scope.set(None, None)

    def test_a_disabled_api_is_no_cluster_and_no_error(self):
        disabled = run_of(1, stderr="ERROR: Kubernetes Engine API has not been used in project acme")
        manifest = fh.collect_fleet(run=lister({"acme": disabled}), query=querier())
        self.assertEqual(manifest["clusters"], [])
        self.assertNotIn("error", manifest)

    def test_a_failed_listing_is_a_gate_failed_row(self):
        manifest = fh.collect_fleet(
            run=lister({"acme": [cluster("a")], "beta": run_of(1, stderr="PERMISSION_DENIED")}),
            query=querier(present=ALL_PRESENT))
        rows = {e["name"]: e["outcome"] for e in manifest["clusters"]}
        self.assertEqual(rows, {"acme/us-central1/a": "collected", "project/beta": "gate-failed"})
        self.assertNotIn("error", manifest)

    def test_no_readable_cluster_sets_the_error(self):
        manifest = fh.collect_fleet(run=lister({"acme": run_of(1, stderr="PERMISSION_DENIED")}), query=querier())
        self.assertIn("no cluster could be read", manifest["error"])

    def test_an_incomplete_listing_keeps_its_clusters_and_flags_the_project(self):
        partial = run_of(0, json.dumps([cluster("a")]), stderr="WARNING: The following zones did not respond: x")
        manifest = fh.collect_fleet(run=lister({"acme": partial}), query=querier(present=ALL_PRESENT))
        self.assertEqual([e["outcome"] for e in manifest["clusters"]], ["collected", "gate-failed"])

    def test_the_declared_scope_is_swept_and_nothing_is_listed(self):
        def run(argv, **kwargs):
            if argv[:4] == ["gcloud", "container", "clusters", "list"]:
                return run_of(0, json.dumps([cluster("a")]))
            raise AssertionError(f"the declared scope is the sweep; nothing should be listed: {argv}")

        fh.declared_scope.set("acme", "beta=denied")
        manifest = fh.collect_fleet(run=run, query=querier(present=ALL_PRESENT))
        self.assertEqual([e["name"] for e in manifest["clusters"]], ["acme/us-central1/a", "project/UNENUMERATED_PROJECTS"])
        self.assertIn("beta (denied)", manifest["clusters"][1]["error"])

    def test_an_excluded_cluster_is_dropped_and_never_reported(self):
        fh.declared_scope.set("acme", None, "acme/us-central1/secret-ops")
        query = querier(present={"container_restart_count": [("a", "us-central1"), ("secret-ops", "us-central1")]},
                        hits={"container_restart_count": [series("secret-ops", value="9", namespace_name="n",
                                                                 pod_name="p", container_name="c")]})
        manifest = fh.collect_fleet(run=lister({"acme": [cluster("a"), cluster("secret-ops")]}), query=query)
        self.assertEqual([e["name"] for e in manifest["clusters"]], ["acme/us-central1/a"])
        self.assertNotIn("secret-ops", json.dumps(manifest["clusters"][0]["candidates"]))

    def test_excluded_clusters_are_kept_out_of_the_metric_reads(self):
        fh.declared_scope.set("acme", None, "acme/us-central1/secret-ops")
        query = querier(present=ALL_PRESENT)
        fh.collect_fleet(run=lister({"acme": [cluster("a"), cluster("secret-ops")]}), query=query)
        for _, promql in query.calls:
            self.assertRegex(promql, r'cluster(_name)?!~"secret-ops"')

    def test_a_matcher_too_long_for_the_relay_falls_back_to_the_post_read_drop(self):
        names = [f"excluded-cluster-with-a-long-name-{i:03d}" for i in range(40)]
        fh.declared_scope.set("acme", None, ",".join(f"acme/us-central1/{n}" for n in names))
        query = querier(present=ALL_PRESENT)
        with contextlib.redirect_stderr(io.StringIO()):
            fh.collect_fleet(run=lister({"acme": [cluster("a")] + [cluster(n) for n in names]}), query=query)
        self.assertNotIn("!~", " ".join(q for _, q in query.calls))
        for _, promql in query.calls:
            self.assertLess(len(promql), fh.MAX_EXCLUDE_MATCHER_CHARS * 2)

    def test_an_excluded_name_an_included_cluster_shares_is_not_matched_out(self):
        fh.declared_scope.set("acme", None, "acme/europe-west1/a")
        query = querier(present=ALL_PRESENT)
        fh.collect_fleet(run=lister({"acme": [cluster("a"), cluster("a", location="europe-west1")]}), query=query)
        self.assertNotIn("!~", " ".join(q for _, q in query.calls))

    def test_no_monitoring_session_keeps_the_control_plane_checks(self):
        with mock.patch.object(fh, "default_session", side_effect=RuntimeError("no ADC")):
            manifest = fh.collect_fleet(run=lister({"acme": [cluster("a", status="ERROR")]}))
        entry = manifest["clusters"][0]
        self.assertEqual(checks(entry), [("cluster-not-running", "critical", "Cluster/a")])
        self.assertEqual({u["check"] for u in entry["checks_unevaluated"]}, set(fh.METRIC_CHECKS))
        self.assertIn("no Cloud Monitoring session", entry["checks_unevaluated"][0]["reason"])

    def test_projects_past_the_deadline_are_gate_failed_rows(self):
        manifest = fh.collect_fleet(run=lister({"acme": [cluster("a")], "beta": [cluster("b")]}),
                                    query=querier(present=ALL_PRESENT), project_budget_s=-1)
        self.assertEqual({e["name"]: e["outcome"] for e in manifest["clusters"]},
                         {"project/acme": "gate-failed", "project/beta": "gate-failed"})
        self.assertIn("--project acme", manifest["clusters"][0]["error"])
        self.assertIn("no cluster could be read", manifest["error"])

    def test_the_project_listing_gets_its_own_timeout(self):
        seen = {}
        inner = lister({"acme": [cluster("a")]})

        def run(argv, **kwargs):
            seen[" ".join(argv[:3])] = kwargs.get("timeout")
            return inner(argv, **kwargs)

        fh.collect_fleet(run=run, query=querier(present=ALL_PRESENT))
        self.assertEqual(seen["gcloud projects list"], fh.PROJECTS_LIST_TIMEOUT_S)

    def test_no_read_starts_past_the_run_budget(self):
        query = querier(present=ALL_PRESENT)
        manifest = fh.collect_fleet(run=lister({"acme": [cluster("a", status="ERROR")]}), query=query, run_budget_s=-1)
        entry = manifest["clusters"][0]
        self.assertEqual(query.calls, [])
        self.assertEqual(checks(entry), [("cluster-not-running", "critical", "Cluster/a")])
        self.assertEqual({u["reason"] for u in entry["checks_unevaluated"]}, {fh.BUDGET_REASON})

    def test_the_clusters_list_gets_only_the_remaining_budget(self):
        seen = {}
        inner = lister({"acme": [cluster("a")]})

        def run(argv, **kwargs):
            if argv[:4] == ["gcloud", "container", "clusters", "list"]:
                seen["timeout"] = kwargs.get("timeout")
            return inner(argv, **kwargs)

        fh.collect_fleet(run=run, query=querier(present=ALL_PRESENT), run_budget_s=10)
        self.assertLessEqual(seen["timeout"], 10)

    def test_a_project_that_raises_does_not_lose_the_fleet(self):
        def query(project, promql):
            if project == "beta":
                raise RuntimeError("boom")
            return [], None

        manifest = fh.collect_fleet(run=lister({"acme": [cluster("a")], "beta": [cluster("b")]}), query=query)
        rows = {e["name"]: e["outcome"] for e in manifest["clusters"]}
        self.assertEqual(rows, {"acme/us-central1/a": "collected", "project/beta": "gate-failed"})

    def test_the_summary_names_every_unevaluated_check(self):
        query = querier(present={"node_status_condition": [("a", "us-central1")],
                                 "container_restart_count": [("a", "us-central1")],
                                 "kube_deployment": [("a", "us-central1")],
                                 "kube_statefulset": [("a", "us-central1")]})
        manifest = fh.collect_fleet(run=lister({"acme": [cluster("a")]}), query=query)
        self.assertEqual(fh.summary_lines(manifest)[1],
                         "not evaluated, so not healthy either -- name each in the answer: pod-pending on 1 "
                         "cluster(s) (acme/us-central1/a): the cluster (status RUNNING) reported no series for this metric")

    def test_the_summary_groups_one_reason_across_many_clusters(self):
        clusters = [cluster(f"c{i}", packages=("SYSTEM_COMPONENTS", "DEPLOYMENT")) for i in range(7)]
        present = {m: [(f"c{i}", "us-central1") for i in range(7)]
                   for m in ("node_status_condition", "container_restart_count", "kube_deployment")}
        manifest = fh.collect_fleet(run=lister({"acme": clusters}), query=querier(present=present))
        line = fh.summary_lines(manifest)[1]
        self.assertIn("pod-pending on 7 cluster(s)", line)
        self.assertIn("and 2 more", line)
        self.assertEqual(line.count("pod-pending"), 1)

    def test_the_summary_counts_clusters_with_a_finding(self):
        manifest = fh.collect_fleet(run=lister({"acme": [cluster("a", status="ERROR"), cluster("b")]}),
                                    query=querier(present=ALL_PRESENT))
        self.assertEqual(fh.summary_lines(manifest)[0],
                         "2 cluster(s) read, 1 with a finding -- cluster-not-running: 1 (acme/us-central1/a)")


class PromqlQueryFnTest(unittest.TestCase):
    class Response:
        def __init__(self, status, body):
            self.status_code, self._body, self.text = status, body, str(body)

        def json(self):
            if isinstance(self._body, Exception):
                raise self._body
            return self._body

    def query_with(self, response):
        session = mock.Mock()
        session.get.return_value = response
        rows, error = fh.promql_query_fn(session)("acme", "up")
        url = session.get.call_args.args[0]
        self.assertEqual(url, "https://monitoring.googleapis.com/v1/projects/acme/location/global/prometheus/api/v1/query")
        return rows, error

    def test_a_success_returns_the_rows(self):
        rows, error = self.query_with(self.Response(200, {"status": "success", "data": {"result": [{"metric": {}}]}}))
        self.assertEqual((rows, error), ([{"metric": {}}], None))

    def test_a_refusal_an_error_and_a_bad_body_are_errors(self):
        self.assertTrue(self.query_with(self.Response(403, "denied"))[1].startswith("HTTP 403"))
        self.assertIn("bad_data", self.query_with(self.Response(200, {"status": "error", "errorType": "bad_data"}))[1])
        self.assertIn("not JSON", self.query_with(self.Response(200, ValueError("x")))[1])


class MainTest(unittest.TestCase):
    def test_help_lists_the_scope_flags(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            fh.main(["--help"])
        for flag in ("--scope-projects", "--scope-unread", "--scope-exclude-clusters", "--project"):
            self.assertIn(flag, out.getvalue())

    def test_output_writes_the_file_whole_and_prints_its_path(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.json")
            err, out = io.StringIO(), io.StringIO()
            with mock.patch.object(fh, "collect_fleet", return_value={"clusters": []}), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(fh.main(["--output", path]), 0)
            self.assertEqual(json.load(open(path)), {"clusters": []})
            self.assertEqual(out.getvalue(), "")
            self.assertIn(f"wrote the manifest to {path}", err.getvalue())
            self.assertFalse(os.path.exists(path + ".partial"))
        fh.declared_scope.set(None, None)

    def test_main_passes_the_scope_flags_to_the_declared_scope(self):
        with mock.patch.object(fh, "collect_fleet", return_value={"clusters": []}) as collect, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(fh.main(["--scope-projects", "acme", "--scope-exclude-clusters", "acme/us-central1/x"]), 0)
        collect.assert_called_once_with(None)
        self.assertEqual(list(fh.declared_scope.projects), ["acme"])
        fh.declared_scope.set(None, None)


class DefaultRunTimeoutTest(unittest.TestCase):
    """`TimeoutExpired` carries the child's output as bytes even under `text=True`;
    every reader of `Run` matches markers against a str, so the timeout arm
    must hand back text, as the other collectors' `default_run` copies do."""

    TIMED_OUT = fh.subprocess.TimeoutExpired(["gcloud"], 1, output=b"partial", stderr=b"WARNING: zone did not respond")

    def test_a_timeout_that_wrote_output_returns_text(self):
        with mock.patch.object(fh.subprocess, "run", side_effect=self.TIMED_OUT):
            result = fh.default_run(["gcloud"])
        self.assertEqual(result.rc, fh.TIMEOUT_RC)
        self.assertEqual((result.stdout, result.stderr), ("partial", "WARNING: zone did not respond"))

    def test_a_timeout_that_wrote_nothing_returns_empty_text(self):
        with mock.patch.object(fh.subprocess, "run", side_effect=fh.subprocess.TimeoutExpired(["gcloud"], 1)):
            result = fh.default_run(["gcloud"])
        self.assertEqual((result.rc, result.stdout, result.stderr), (fh.TIMEOUT_RC, "", ""))

    def test_a_completed_run_passes_its_text_through(self):
        done = fh.subprocess.CompletedProcess(["gcloud"], 0, stdout="[]", stderr="")
        with mock.patch.object(fh.subprocess, "run", return_value=done):
            result = fh.default_run(["gcloud"])
        self.assertEqual((result.rc, result.stdout, result.stderr), (0, "[]", ""))

    def test_a_timed_out_clusters_list_reads_as_a_gate_failure_in_gclouds_words(self):
        with mock.patch.object(fh.subprocess, "run", side_effect=self.TIMED_OUT):
            timed_out = fh.default_run(["gcloud"])
        with contextlib.redirect_stderr(io.StringIO()):
            clusters, record, error = fh.list_clusters("proj-a", run=lambda argv, **kw: timed_out)
        self.assertEqual((clusters, record), ([], None))
        self.assertEqual(error, f"clusters list rc={fh.TIMEOUT_RC}: WARNING: zone did not respond")

    def test_a_timed_out_projects_list_is_quoted_as_text_not_as_a_bytes_repr(self):
        """`discover_fleet` formats the listing's stderr into the discovery
        error with an f-string, which renders bytes as `b'...'` without
        raising; the error must carry gcloud's words."""
        with mock.patch.object(fh.subprocess, "run", side_effect=self.TIMED_OUT):
            timed_out = fh.default_run(["gcloud"])

        def run(argv, **_):
            if argv[:3] == ["gcloud", "config", "get-value"]:
                return fh.Run(argv, 1, "", "no active project", 0.0)
            return timed_out

        fh.declared_scope.set(None, None)
        discovery = fh.discover_fleet(None, run=run)
        self.assertEqual(discovery.projects, [])
        self.assertEqual(discovery.error, f"project discovery failed: `gcloud projects list` rc={fh.TIMEOUT_RC}: WARNING: zone did not respond")


if __name__ == "__main__":
    unittest.main()
