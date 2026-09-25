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
    ApiRoute,
    evaluate,
)

MONITORING = "monitoring.googleapis.com"


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
            self.assertIn(route.rule_id, decision.message)

    def test_the_project_grammar_is_googles(self):
        pattern = re.compile(rf"^{PROJECT}$")
        for project in ("kagents-dev", "abcdef", "a" + "b" * 28 + "c", "p1-2-3"):
            with self.subTest(project=project):
                self.assertIsNotNone(pattern.match(project))
        for project in ("abcde", "1abcdef", "abcdef-", "Abcdef", "a" * 31, "ab.cdef", "ab/cdef", "ab cdef"):
            with self.subTest(project=project):
                self.assertIsNone(pattern.match(project))


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
