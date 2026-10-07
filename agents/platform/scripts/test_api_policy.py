#!/usr/bin/env python3
"""Tests for the read-only Cloud API relay's policy table.

The table is the whole of the enforcement between the sandbox and every REST
read the platform service account can make, so each test here holds one door:
a route is admitted with its rule id, and the same shape one word away is not.

Run:  python3 agents/platform/scripts/test_api_policy.py
"""

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
        # Logging is the next entry the design expects; until it exists the
        # refusal says so, and says it differently from the token endpoints.
        decision = evaluate("GET", "logging.googleapis.com", "v2/entries", "")
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
        with self.assertRaises(ValueError):
            api_policy._validate_routes((self.route(method="POST"),))

    def test_an_empty_rule_id_raises(self):
        with self.assertRaises(ValueError):
            api_policy._validate_routes((self.route(rule_id=""),))


if __name__ == "__main__":
    unittest.main()
