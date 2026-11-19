#!/usr/bin/env python3
"""Tests for the read-only Cloud API relay's policy table.

The table is the whole of the enforcement between the sandbox and every REST
read the platform service account can make, so each test here holds one door:
a route is admitted with its rule id, and the same shape one word away is not.

Run:  python3 agents/platform/scripts/test_api_policy.py
"""

import json
import re
import unittest

import api_policy
from api_policy import (
    API_READ_ROUTES,
    PROJECT,
    REFUSED_HOSTS,
    TRACE_ID,
    ApiRoute,
    evaluate,
)

MONITORING = "monitoring.googleapis.com"
CLOUDTRACE = "cloudtrace.googleapis.com"
LOGGING = "logging.googleapis.com"
ENTRIES_LIST = "v2/entries:list"
ENTRIES_LIST_RULE = "gcp.api.logging.entries-list"
A_TRACE_ID = "0006344377aac15d1baede1a41e88a2c"


class ListedRoutesTest(unittest.TestCase):
    """Each entry in the table admits exactly its read."""

    CASES = (
        ("v3/projects/kagents-dev/timeSeries", "gcp.api.monitoring.timeseries-list"),
        ("v3/projects/kagents-dev/metricDescriptors", "gcp.api.monitoring.metricdescriptors-list"),
        ("v1/projects/kagents-dev/location/global/prometheus/api/v1/query", "gcp.api.monitoring.promql-read"),
        ("v1/projects/kagents-dev/location/global/prometheus/api/v1/query_range", "gcp.api.monitoring.promql-read"),
        ("v1/projects/kagents-dev/location/global/prometheus/api/v1/series", "gcp.api.monitoring.promql-read"),
        ("v1/projects/kagents-dev/location/global/prometheus/api/v1/labels", "gcp.api.monitoring.promql-read"),
    )

    def test_each_listed_route_is_allowed_with_its_rule_id(self):
        for path, rule_id in self.CASES:
            with self.subTest(path=path):
                decision = evaluate("GET", MONITORING, path, "filter=x")
                self.assertTrue(decision.allowed, decision.message)
                self.assertEqual(rule_id, decision.rule_id)

    def test_the_same_path_with_post_is_refused_on_method(self):
        for path, _ in self.CASES:
            with self.subTest(path=path):
                decision = evaluate("POST", MONITORING, path, "")
                self.assertFalse(decision.allowed)
                self.assertEqual("gcp.api.method", decision.rule_id)

    def test_every_write_method_is_refused_before_the_host_is_read(self):
        # Method first: a write to a refused host reports the method, which is
        # the order the design's security table promises.
        for method in ("POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "get"):
            with self.subTest(method=method):
                decision = evaluate(method, "iamcredentials.googleapis.com", "v1/x", "")
                self.assertEqual("gcp.api.method", decision.rule_id)

    def test_the_host_is_matched_case_insensitively(self):
        decision = evaluate("GET", "Monitoring.googleapis.com", "v3/projects/kagents-dev/timeSeries", "")
        self.assertTrue(decision.allowed)

    def test_the_query_does_not_change_the_decision(self):
        for query in ("", "key=abc", "filter=metric.type%3D%22x%22&pageSize=1000"):
            with self.subTest(query=query):
                self.assertTrue(
                    evaluate("GET", MONITORING, "v3/projects/kagents-dev/timeSeries", query).allowed
                )


class RefusedHostsTest(unittest.TestCase):
    """A token endpoint is refused before the table is consulted, on any path."""

    def test_every_refused_host_is_refused_for_any_path(self):
        for host in sorted(REFUSED_HOSTS):
            for path in ("", "v1/token", "v3/projects/kagents-dev/timeSeries"):
                with self.subTest(host=host, path=path):
                    decision = evaluate("GET", host, path, "")
                    self.assertFalse(decision.allowed)
                    self.assertEqual("gcp.api.host-refused", decision.rule_id)

    def test_an_unlisted_host_is_refused_as_unknown(self):
        # Compute is a host no entry names; the refusal says so, and says it
        # differently from the token endpoints.
        decision = evaluate("GET", "compute.googleapis.com", "compute/v1/projects/kagents-dev/regions", "")
        self.assertFalse(decision.allowed)
        self.assertEqual("gcp.api.host", decision.rule_id)
        self.assertIn("API_READ_ROUTES", decision.message)

    def test_a_host_that_merely_contains_a_listed_one_is_unknown(self):
        for host in (
            "monitoring.googleapis.com.evil.example",
            "evilmonitoring.googleapis.com",
            "xmonitoring.googleapis.com",
        ):
            with self.subTest(host=host):
                self.assertEqual("gcp.api.host", evaluate("GET", host, "v3/projects/kagents-dev/timeSeries", "").rule_id)


class PathShapeTest(unittest.TestCase):
    """On a known host, the anchored regex admits nothing one word away."""

    def test_sibling_and_child_paths_are_refused(self):
        for path in (
            "v3/projects/kagents-dev/timeSeries/x",
            "v3/projects/kagents-dev/timeSeries:query",
            "v3/projects/kagents-dev/alertPolicies",
            "v3/projects/P-UPPER/timeSeries",
            "v3/projects/a/b/timeSeries",
            "v3/projects/kagents-dev/metricDescriptors/kubernetes.io%2Fcontainer%2Fcpu",
            "v3/projects/kagents-dev/metricDescriptors/",
            "/v3/projects/kagents-dev/timeSeries",
            "xv3/projects/kagents-dev/timeSeries",
            "v3/projects/kagents-dev/timeSeries\n",
            "v1/projects/kagents-dev/location/global/prometheus/api/v1/admin",
            "v1/projects/kagents-dev/location/us-central1/prometheus/api/v1/query",
            "",
        ):
            with self.subTest(path=path):
                decision = evaluate("GET", MONITORING, path, "")
                self.assertFalse(decision.allowed)
                self.assertEqual("gcp.api.path", decision.rule_id)

    def test_the_path_refusal_names_what_the_host_does_relay(self):
        decision = evaluate("GET", MONITORING, "v3/projects/kagents-dev/alertPolicies", "")
        for route in API_READ_ROUTES:
            if route.host == MONITORING:
                self.assertIn(route.rule_id, decision.message)
            else:
                self.assertNotIn(route.rule_id, decision.message)

    def test_the_project_grammar_is_googles(self):
        pattern = re.compile(rf"^{PROJECT}$")
        for project in ("kagents-dev", "abcdef", "a" + "b" * 28 + "c", "p1-2-3", "123456789012", "1", "9" * 19):
            with self.subTest(project=project):
                self.assertIsNotNone(pattern.match(project))
        for project in (
            "abcde", "1abcdef", "abcdef-", "Abcdef", "a" * 31, "ab.cdef", "ab/cdef", "ab cdef",
            # A project number is digits alone: no leading zero, nothing an
            # int64 cannot hold, no sign, no letter after the digits.
            "0123", "1" * 20, "-123456", "123456a", "12 3456",
        ):
            with self.subTest(project=project):
                self.assertIsNone(pattern.match(project))

    def test_a_project_number_is_admitted_on_every_route(self):
        # An install whose PlatformAgent projectId is the project number sends
        # it in the project position; the relay takes it the way `gcloud` does.
        for host, path in (
            (MONITORING, "v3/projects/123456789012/timeSeries"),
            (MONITORING, "v3/projects/123456789012/metricDescriptors"),
            (MONITORING, "v1/projects/123456789012/location/global/prometheus/api/v1/query"),
            (CLOUDTRACE, "v1/projects/123456789012/traces"),
            (CLOUDTRACE, f"v1/projects/123456789012/traces/{A_TRACE_ID}"),
        ):
            with self.subTest(path=path):
                self.assertTrue(evaluate("GET", host, path, "").allowed, path)


class CloudTraceRoutesTest(unittest.TestCase):
    """The two Trace reads the observability helpers make, and nothing beside them."""

    CASES = (
        ("v1/projects/kagents-dev/traces", "gcp.api.cloudtrace.traces-list"),
        (f"v1/projects/kagents-dev/traces/{A_TRACE_ID}", "gcp.api.cloudtrace.traces-get"),
    )

    def test_each_trace_route_is_allowed_with_its_rule_id(self):
        for path, rule_id in self.CASES:
            with self.subTest(path=path):
                decision = evaluate("GET", CLOUDTRACE, path, "startTime=x&pageSize=5")
                self.assertTrue(decision.allowed, decision.message)
                self.assertEqual(rule_id, decision.rule_id)

    def test_the_same_path_with_post_is_refused_on_method(self):
        for path, _ in self.CASES:
            with self.subTest(path=path):
                decision = evaluate("POST", CLOUDTRACE, path, "")
                self.assertFalse(decision.allowed)
                self.assertEqual("gcp.api.method", decision.rule_id)

    def test_sibling_child_and_malformed_trace_paths_are_refused(self):
        for path in (
            "v1/projects/kagents-dev/traces/",
            f"v1/projects/kagents-dev/traces/{A_TRACE_ID}/spans",
            f"v1/projects/kagents-dev/traces/{A_TRACE_ID}/",
            "v1/projects/kagents-dev/traces/not-a-trace-id",
            "v1/projects/kagents-dev/traces/" + "g" * 32,
            "v1/projects/kagents-dev/traces/" + "a" * 31,
            "v1/projects/kagents-dev/traces/" + "a" * 33,
            "v1/projects/kagents-dev/traces/" + A_TRACE_ID.upper(),
            "v1/projects/kagents-dev/traces:batchWrite",
            "v1/projects/kagents-dev/tracesx",
            "v1/projects/P-UPPER/traces",
            "v1/projects/a/b/traces",
            f"v2/projects/kagents-dev/traces/{A_TRACE_ID}",
            "v2/projects/kagents-dev/traces",
            "v1/projects/kagents-dev/traces\n",
            "",
        ):
            with self.subTest(path=path):
                decision = evaluate("GET", CLOUDTRACE, path, "")
                self.assertFalse(decision.allowed)
                self.assertEqual("gcp.api.path", decision.rule_id)

    def test_the_trace_refusal_names_only_the_trace_rules(self):
        decision = evaluate("GET", CLOUDTRACE, "v2/projects/kagents-dev/traces", "")
        self.assertIn("gcp.api.cloudtrace.traces-list", decision.message)
        self.assertIn("gcp.api.cloudtrace.traces-get", decision.message)
        self.assertNotIn("gcp.api.monitoring", decision.message)

    def test_a_trace_path_on_the_monitoring_host_is_refused(self):
        decision = evaluate("GET", MONITORING, "v1/projects/kagents-dev/traces", "")
        self.assertFalse(decision.allowed)
        self.assertEqual("gcp.api.path", decision.rule_id)

    def test_the_trace_id_grammar_is_32_lower_case_hex(self):
        # `fullmatch`, as `evaluate` uses it: `$` alone would admit a trailing newline.
        pattern = re.compile(TRACE_ID)
        self.assertIsNotNone(pattern.fullmatch(A_TRACE_ID))
        for trace_id in ("", "a" * 31, "a" * 33, A_TRACE_ID.upper(), "g" * 32, A_TRACE_ID + "\n"):
            with self.subTest(trace_id=trace_id):
                self.assertIsNone(pattern.fullmatch(trace_id))


CONTAINER = "container.googleapis.com"


def _verdict(decision):
    return decision.allowed, decision.rule_id


class GkeClusterRoutesTest(unittest.TestCase):
    """#3703: the reconcile's two GKE reads, approved as exactly these two, and projected."""

    LIST = "v1/projects/kagents-dev/locations/-/clusters"
    GET = "v1/projects/kagents-dev/locations/us-central1-a/clusters/seeded-a"

    def test_each_cluster_route_is_allowed_with_its_rule_id(self):
        for path, rule_id in (
            (self.LIST, "gcp.api.container.clusters-list"),
            ("v1/projects/123456789012/locations/us-central1/clusters", "gcp.api.container.clusters-list"),
            (self.GET, "gcp.api.container.clusters-get"),
            ("v1/projects/kagents-dev/locations/us-central1/clusters/a", "gcp.api.container.clusters-get"),
            ("v1/projects/kagents-dev/locations/europe-west4-c/clusters/" + "a" * 40, "gcp.api.container.clusters-get"),
        ):
            with self.subTest(path=path):
                self.assertEqual((True, rule_id), _verdict(evaluate("GET", CONTAINER, path, "")))

    def test_writes_on_the_cluster_paths_are_refused_on_method(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            for path in (self.LIST, self.GET):
                with self.subTest(method=method, path=path):
                    self.assertEqual((False, "gcp.api.method"), _verdict(evaluate(method, CONTAINER, path, "")))

    def test_every_other_container_path_is_refused(self):
        for path in (
            self.GET + "/nodePools",
            self.GET + "/nodePools/default-pool",
            self.GET + ":getJwks",
            self.GET + ":setLabels",
            self.LIST + "/",
            self.GET + "/",
            "v1/projects/kagents-dev/locations/-/operations",
            "v1/projects/kagents-dev/locations/-/serverConfig",
            "v1/projects/kagents-dev/zones/us-central1-a/clusters",
            "v1beta1/projects/kagents-dev/locations/-/clusters",
            "v1/projects/kagents-dev/locations/US-central1/clusters",
            "v1/projects/kagents-dev/locations/us-central1:x/clusters",
            "v1/projects/kagents-dev/locations/us-central1-ab/clusters",
            "v1/projects/kagents-dev/locations/us-central123/clusters",
            "v1/projects/kagents-dev/locations/uscentral1/clusters",
            "v1/projects/kagents-dev/locations/global/clusters",
            "v1/projects/kagents-dev/locations/--/clusters",
            "v1/projects/kagents-dev/locations/us-central1-/clusters",
            "v1/projects/kagents-dev/locations/us-central1/clusters/Seeded-a",
            "v1/projects/kagents-dev/locations/us-central1/clusters/1a",
            "v1/projects/kagents-dev/locations/us-central1/clusters/a-",
            "v1/projects/kagents-dev/locations/us-central1/clusters/" + "a" * 41,
            "v1/projects/kagents-dev/locations/us-central1/clusters/a/b",
            "v1/projects/kagents-dev/locations/us-central1/clusters/a%2Fb",
            "v1/projects/kagents-dev/locations/us-central1/clusters/a\n",
            "v1/projects/kagents-dev/locations/-/clusters/../operations",
            "v1/projects/kagents-dev/aggregated/usableSubnetworks",
        ):
            with self.subTest(path=path):
                self.assertFalse(evaluate("GET", CONTAINER, path, "").allowed)

    def test_the_cluster_shapes_are_not_reachable_on_another_host(self):
        for host in (MONITORING, CLOUDTRACE, "gkehub.googleapis.com"):
            with self.subTest(host=host):
                self.assertFalse(evaluate("GET", host, self.GET, "").allowed)

    def test_both_projections_drop_every_credential_field(self):
        master_auth = {"clusterCaCertificate": "CA", "fieldOne": "CERT", "fieldTwo": "KEY",
                       "fieldThree": "admin", "fieldFour": "hunter2"}
        cluster = {"name": "a", "location": "l", "status": "RUNNING", "id": "1", "endpoint": "34.1.2.3",
                   "masterAuth": master_auth, "privateClusterConfig": {"privateEndpoint": "10.0.0.2"}}
        got_get = api_policy.project_gke_cluster(json.dumps(cluster).encode())
        got_list = api_policy.project_gke_cluster_list(json.dumps({"clusters": [cluster]}).encode())
        self.assertEqual({"name": "a", "location": "l", "status": "RUNNING", "id": "1"}, json.loads(got_get))
        self.assertEqual({"clusters": [{"name": "a", "location": "l", "status": "RUNNING", "id": "1"}]},
                         json.loads(got_list))
        for body in (got_get, got_list):
            for secret in (b"CERT", b"KEY", b"hunter2", b"masterAuth", b"endpoint", b"10.0.0.2"):
                self.assertNotIn(secret, body)

    def test_a_projection_refuses_what_it_cannot_read(self):
        for raw in (b"not json", b"[]", b"7", b'"s"'):
            with self.subTest(raw=raw):
                self.assertIsNone(api_policy.project_gke_cluster(raw))
                self.assertIsNone(api_policy.project_gke_cluster_list(raw))
        self.assertIsNone(api_policy.project_gke_cluster_list(b'{"clusters": {"a": 1}}'))
        self.assertIsNone(api_policy.project_gke_cluster_list(b'{"clusters": ["a"]}'))
        self.assertEqual(b'{"clusters":[]}', api_policy.project_gke_cluster_list(b"{}"))

    def test_a_list_keeps_no_top_level_key_but_clusters_and_missing_zones(self):
        body = {"clusters": [], "missingZones": ["europe-west1-b"], "nextPageToken": "t", "fieldOne": "x"}
        got = json.loads(api_policy.project_gke_cluster_list(json.dumps(body).encode()))
        self.assertEqual({"clusters": [], "missingZones": ["europe-west1-b"]}, got)

    def test_only_the_cluster_routes_carry_a_projection(self):
        projected = {r.rule_id for r in api_policy.API_READ_ROUTES if r.project is not None}
        self.assertEqual({"gcp.api.container.clusters-list", "gcp.api.container.clusters-get"}, projected)
        self.assertIs(api_policy.project_gke_cluster, api_policy.response_projection("gcp.api.container.clusters-get"))
        self.assertIsNone(api_policy.response_projection("gcp.api.monitoring.timeseries-list"))


class LoggingEntriesListRouteTest(unittest.TestCase):
    """The one POST the relay forwards, and every door one word from it."""

    BODY = {
        "resourceNames": ["projects/kagents-dev"],
        "filter": 'resource.type="k8s_cluster"',
        "orderBy": "timestamp desc",
        "pageSize": 1000,
        "pageToken": "token",
    }

    def test_the_route_is_allowed_with_its_rule_id_and_carries_a_body(self):
        decision = evaluate("POST", LOGGING, ENTRIES_LIST, "")
        self.assertTrue(decision.allowed, decision.message)
        self.assertEqual(ENTRIES_LIST_RULE, decision.rule_id)
        self.assertTrue(api_policy.carries_body(decision.rule_id))

    def test_it_is_the_only_route_that_carries_a_body(self):
        self.assertEqual(
            [ENTRIES_LIST_RULE], [r.rule_id for r in API_READ_ROUTES if api_policy.carries_body(r.rule_id)]
        )
        self.assertEqual([ENTRIES_LIST_RULE], [r.rule_id for r in API_READ_ROUTES if r.method == "POST"])

    def test_every_neighbour_post_on_the_logging_host_is_refused_on_method(self):
        for path in (
            "v2/entries:write",
            "v2/entries:tail",
            "v2/entries:copy",
            "v2/entries:list/",
            "v2/entries:listx",
            "v2/entries:list\n",
            "v2/entries%3Alist",
            "v2/entries",
            "v2beta1/entries:list",
            "v1/entries:list",
            "v2/projects/kagents-dev/sinks",
            "v2/projects/kagents-dev/exclusions",
            "v2/projects/kagents-dev/logs",
            "v2/projects/kagents-dev/locations/global/buckets/_Default/views/_AllLogs",
        ):
            with self.subTest(path=path):
                decision = evaluate("POST", LOGGING, path, "")
                self.assertFalse(decision.allowed)
                self.assertEqual("gcp.api.method", decision.rule_id)

    def test_the_route_is_a_post_and_no_other_method(self):
        decision = evaluate("GET", LOGGING, ENTRIES_LIST, "")
        self.assertFalse(decision.allowed)
        self.assertEqual("gcp.api.path", decision.rule_id)
        for method in ("PUT", "PATCH", "DELETE", "HEAD", "post"):
            with self.subTest(method=method):
                self.assertEqual("gcp.api.method", evaluate(method, LOGGING, ENTRIES_LIST, "").rule_id)

    def test_the_refusal_of_the_route_as_a_get_says_the_read_is_a_post(self):
        message = evaluate("GET", LOGGING, ENTRIES_LIST, "").message
        self.assertIn(f"{ENTRIES_LIST_RULE} (POST)", message)
        self.assertIn("this GET path", message)

    def test_no_logging_path_is_a_get_route(self):
        for path in ("v2/projects/kagents-dev/sinks", "v2/projects/kagents-dev/logs", "v2/entries"):
            with self.subTest(path=path):
                decision = evaluate("GET", LOGGING, path, "")
                self.assertFalse(decision.allowed)
                self.assertEqual("gcp.api.path", decision.rule_id)

    def test_a_post_on_every_get_route_is_still_refused_on_method(self):
        # A POST on Monitoring `timeSeries` is a write, and one on the GKE
        # `clusters` collection creates a cluster.
        for host, path in (
            (MONITORING, "v3/projects/kagents-dev/timeSeries"),
            (MONITORING, "v3/projects/kagents-dev/timeSeries:query"),
            (CLOUDTRACE, "v1/projects/kagents-dev/traces"),
            ("container.googleapis.com", "v1/projects/kagents-dev/locations/-/clusters"),
            (MONITORING, ENTRIES_LIST),
            ("iamcredentials.googleapis.com", ENTRIES_LIST),
            ("evil.example.com", ENTRIES_LIST),
        ):
            with self.subTest(host=host, path=path):
                decision = evaluate("POST", host, path, "")
                self.assertFalse(decision.allowed)
                self.assertEqual("gcp.api.method", decision.rule_id)

    def test_the_route_takes_no_query(self):
        for query in ("fields=entries", "alt=media", "resourceNames=projects/other", "x"):
            with self.subTest(query=query):
                decision = evaluate("POST", LOGGING, ENTRIES_LIST, query)
                self.assertFalse(decision.allowed)
                self.assertEqual("gcp.api.body", decision.rule_id)

    def test_the_body_gcloud_sends_is_allowed(self):
        for body in (
            self.BODY,
            {"resourceNames": ["projects/kagents-dev"]},
            {"resourceNames": ["projects/123456789012"], "filter": "", "orderBy": "timestamp asc", "pageSize": 1},
        ):
            with self.subTest(body=body):
                decision = api_policy.evaluate_body(ENTRIES_LIST_RULE, body)
                self.assertTrue(decision.allowed, decision.message)
                self.assertEqual(ENTRIES_LIST_RULE, decision.rule_id)

    def test_a_body_that_names_anything_but_one_project_is_refused(self):
        for names in (
            None,
            [],
            "projects/kagents-dev",
            ["projects/kagents-dev", "projects/other-project"],
            ["folders/123456789012"],
            ["organizations/123456789012"],
            ["billingAccounts/0X0X0X-0X0X0X-0X0X0X"],
            ["projects/kagents-dev/locations/global/buckets/_Default/views/_AllLogs"],
            ["projects/kagents-dev/logs/syslog"],
            ["projects/KAGENTS-DEV"],
            ["projects/kagents-dev\n"],
            ["projects/"],
            [["projects/kagents-dev"]],
            [7],
        ):
            with self.subTest(names=names):
                body = {**self.BODY, "resourceNames": names}
                if names is None:
                    del body["resourceNames"]
                decision = api_policy.evaluate_body(ENTRIES_LIST_RULE, body)
                self.assertFalse(decision.allowed)
                self.assertEqual("gcp.api.body", decision.rule_id)

    def test_a_key_the_route_does_not_name_is_refused(self):
        # `projectIds` is the API's deprecated second way to name a project.
        for key in ("projectIds", "resource_names", "ResourceNames", "fields", ""):
            with self.subTest(key=key):
                decision = api_policy.evaluate_body(ENTRIES_LIST_RULE, {**self.BODY, key: ["other-project"]})
                self.assertFalse(decision.allowed)
                self.assertEqual("gcp.api.body", decision.rule_id)

    def test_a_value_of_the_wrong_type_or_range_is_refused(self):
        for key, value in (
            ("filter", 7),
            ("filter", ["x"]),
            ("filter", None),
            ("orderBy", "timestamp"),
            ("orderBy", "timestamp desc, insertId"),
            ("orderBy", ["timestamp desc"]),
            ("orderBy", None),
            ("pageSize", 0),
            ("pageSize", 1001),
            ("pageSize", -1),
            ("pageSize", True),
            ("pageSize", 1.5),
            ("pageSize", "1000"),
            ("pageSize", None),
            ("pageToken", 7),
            ("pageToken", {"x": 1}),
        ):
            with self.subTest(key=key, value=value):
                decision = api_policy.evaluate_body(ENTRIES_LIST_RULE, {**self.BODY, key: value})
                self.assertFalse(decision.allowed)
                self.assertEqual("gcp.api.body", decision.rule_id)

    def test_a_body_that_is_not_an_object_is_refused(self):
        for body in (None, [], [self.BODY], "x", 7, True):
            with self.subTest(body=body):
                self.assertEqual("gcp.api.body", api_policy.evaluate_body(ENTRIES_LIST_RULE, body).rule_id)

    def test_a_refusal_never_repeats_the_callers_text(self):
        marker = "caller-text-marker"
        for body in (
            {**self.BODY, marker: 1},
            {**self.BODY, "resourceNames": [marker]},
            {**self.BODY, "orderBy": marker},
            {**self.BODY, "pageToken": [marker]},
        ):
            with self.subTest(body=body):
                self.assertNotIn(marker, api_policy.evaluate_body(ENTRIES_LIST_RULE, body).message)

    def test_in_a_custom_universe_the_route_is_that_universes_logging_host(self):
        universe = "apis-tpczero.example"
        own = evaluate("POST", f"logging.{universe}", ENTRIES_LIST, "", universe_domain=universe)
        self.assertTrue(own.allowed, own.message)
        self.assertEqual(ENTRIES_LIST_RULE, own.rule_id)
        # The commercial host is another universe's: a token must not cross to it.
        for host in (LOGGING, f"logging.other.{universe}", f"monitoring.{universe}"):
            with self.subTest(host=host):
                crossed = evaluate("POST", host, ENTRIES_LIST, "", universe_domain=universe)
                self.assertFalse(crossed.allowed)
                self.assertEqual("gcp.api.method", crossed.rule_id)
        self.assertEqual(
            "gcp.api.body",
            evaluate("POST", f"logging.{universe}", ENTRIES_LIST, "fields=x", universe_domain=universe).rule_id,
        )
        # In the default universe that universe's host is not a route.
        self.assertEqual("gcp.api.method", evaluate("POST", f"logging.{universe}", ENTRIES_LIST, "").rule_id)

    def test_a_malformed_universe_refuses_the_route(self):
        for universe in ("not a domain", "UPPER.example/x", ".."):
            with self.subTest(universe=universe):
                decision = evaluate("POST", LOGGING, ENTRIES_LIST, "", universe_domain=universe)
                self.assertFalse(decision.allowed)
                self.assertEqual("gcp.api.method", decision.rule_id)

    def test_a_domain_scoped_project_is_one_project(self):
        # The project grammar the path routes use admits a `<prefix>:<project>` id.
        body = {**self.BODY, "resourceNames": ["projects/eu0:kagents-dev"]}
        self.assertTrue(api_policy.evaluate_body(ENTRIES_LIST_RULE, body).allowed)
        for names in (
            ["projects/eu0:kagents-dev:x"],
            ["projects/eu0:"],
            ["projects/:kagents-dev"],
            ["projects/a:b/c"],
            ["projects/eu0:eu1:kagents-dev"],
            ["projects/a/b:kagents-dev"],
        ):
            with self.subTest(names=names):
                self.assertFalse(api_policy.evaluate_body(ENTRIES_LIST_RULE, {**self.BODY, "resourceNames": names}).allowed)

    def test_the_resource_of_an_allowed_body_is_its_one_project(self):
        self.assertEqual("projects/kagents-dev", api_policy.body_resource(ENTRIES_LIST_RULE, self.BODY))
        self.assertEqual(
            "projects/123456789012",
            api_policy.body_resource(ENTRIES_LIST_RULE, {"resourceNames": ["projects/123456789012"]}),
        )

    def test_a_refused_body_has_no_resource(self):
        # A name the check did not pass is caller text: nothing of it comes back,
        # whatever is wrong with the body, and nothing raises.
        for body in (
            {**self.BODY, "resourceNames": ["projects/kagents-dev", "projects/other-project"]},
            {**self.BODY, "resourceNames": ["folders/123"]},
            {**self.BODY, "resourceNames": ["projects/kagents-dev\n"]},
            {**self.BODY, "resourceNames": ["projects/kagents-dev resource=projects/other-project"]},
            {**self.BODY, "resourceNames": "projects/kagents-dev"},
            {**self.BODY, "projectIds": ["kagents-dev"]},
            {**self.BODY, "pageSize": 100000},
            {"filter": "x"},
            [], None, "projects/kagents-dev", 7,
        ):
            with self.subTest(body=body):
                self.assertIsNone(api_policy.body_resource(ENTRIES_LIST_RULE, body))

    def test_a_route_whose_body_names_no_resource_has_none(self):
        self.assertIsNone(api_policy.body_resource("gcp.api.monitoring.timeseries-list", self.BODY))
        self.assertIsNone(api_policy.body_resource("no.such.rule", self.BODY))

    def test_a_route_with_no_body_check_refuses_every_body(self):
        decision = api_policy.evaluate_body("gcp.api.monitoring.timeseries-list", self.BODY)
        self.assertFalse(decision.allowed)
        self.assertEqual("gcp.api.body", decision.rule_id)


class TableValidatorTest(unittest.TestCase):
    """The import-time check refuses a table that cannot enforce what it says."""

    GOOD = re.compile(rf"^v3/projects/{PROJECT}/timeSeries$")

    def route(self, **overrides):
        fields = dict(host=MONITORING, method="GET", path=self.GOOD, rule_id="t.one")
        fields.update(overrides)
        return ApiRoute(**fields)

    def test_the_shipped_table_validates(self):
        api_policy._validate_routes(API_READ_ROUTES)

    def test_a_host_with_a_scheme_raises(self):
        for host in ("https://monitoring.googleapis.com", "monitoring.googleapis.com:443",
                     "monitoring.googleapis.com/v3", "Monitoring.googleapis.com", "localhost", ""):
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    api_policy._validate_routes((self.route(host=host),))

    def test_a_refused_host_cannot_be_added_to_the_table(self):
        with self.assertRaises(ValueError):
            api_policy._validate_routes((self.route(host="iamcredentials.googleapis.com"),))

    def test_an_unanchored_regex_raises(self):
        for pattern in (r"v3/projects/p/timeSeries$", r"^v3/projects/p/timeSeries", r"timeSeries"):
            with self.subTest(pattern=pattern):
                with self.assertRaises(ValueError):
                    api_policy._validate_routes((self.route(path=re.compile(pattern)),))

    def test_a_leading_slash_raises(self):
        with self.assertRaises(ValueError):
            api_policy._validate_routes((self.route(path=re.compile(r"^/v3/x$")),))

    def test_a_string_path_raises(self):
        with self.assertRaises(TypeError):
            api_policy._validate_routes((self.route(path=r"^v3/x$"),))

    def test_a_duplicate_rule_id_raises(self):
        with self.assertRaises(ValueError):
            api_policy._validate_routes((self.route(), self.route(path=re.compile(r"^v3/y$"))))

    def test_a_write_method_raises(self):
        for method in ("PUT", "PATCH", "DELETE", "post"):
            with self.subTest(method=method), self.assertRaises(ValueError):
                api_policy._validate_routes((self.route(method=method),))

    def test_a_post_route_without_a_body_check_raises(self):
        with self.assertRaises(ValueError):
            api_policy._validate_routes((self.route(method="POST"),))

    def test_a_get_route_with_a_body_check_raises(self):
        with self.assertRaises(ValueError):
            api_policy._validate_routes((self.route(body=lambda body: None),))

    def test_a_body_check_that_is_not_callable_raises(self):
        with self.assertRaises(TypeError):
            api_policy._validate_routes((self.route(method="POST", body="check"),))

    def test_a_post_route_with_a_body_check_validates(self):
        api_policy._validate_routes((self.route(method="POST", body=lambda body: None),))

    def test_an_empty_rule_id_raises(self):
        with self.assertRaises(ValueError):
            api_policy._validate_routes((self.route(rule_id=""),))


if __name__ == "__main__":
    unittest.main()
