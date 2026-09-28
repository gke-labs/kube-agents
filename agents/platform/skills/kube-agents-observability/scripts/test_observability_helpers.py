#!/usr/bin/env python3
"""Tests for the observability helpers' route to Google: the broker, never a token.

Each helper is run against `credential_proxy_client.ApiSession` over a fake
HTTP layer (the FakeHttp pattern in test_credential_proxy_client.py), so what
is asserted is the relayed URL the broker would see, the query it carries, the
caller header on it, and the helper's output. A second set of checks ties the
helpers to the policy modules they depend on: every relayed read is one
`api_policy` admits, the `gcloud logging read` argv is one `command_policy`
admits, and no helper's source names a token fetch or a bearer header.

Run:  python3 -m unittest discover -p "test_*.py"  (in this directory)
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

import google_api  # puts agents/platform/scripts on sys.path

import analyze_trace_latency
import api_policy
import check_token_usage
import command_policy
import credential_proxy_client
import fetch_traces
import get_chat_users
import get_metric_descriptors

HERE = Path(__file__).resolve().parent
ENDPOINT = "http://127.0.0.1:8765"
PROJECT = "kagents-dev"
# What argparse exits with on a refused argument.
ARGPARSE_USAGE_EXIT = 2
TRACE_A = "0006344377aac15d1baede1a41e88a2c"
TRACE_B = "ffffffffffffffffffffffffffffffff"
RELAYED_TRACES = f"{ENDPOINT}/v1/gcp/cloudtrace.googleapis.com/v1/projects/{PROJECT}/traces"
RELAYED_TIME_SERIES = f"{ENDPOINT}/v1/gcp/monitoring.googleapis.com/v3/projects/{PROJECT}/timeSeries"
RELAYED_DESCRIPTORS = f"{ENDPOINT}/v1/gcp/monitoring.googleapis.com/v3/projects/{PROJECT}/metricDescriptors"
HELPERS = (
    "analyze_trace_latency.py",
    "check_token_usage.py",
    "fetch_traces.py",
    "get_chat_users.py",
    "get_metric_descriptors.py",
    "google_api.py",
)

TRACE_LIST = {"traces": [{"traceId": TRACE_A, "projectId": PROJECT}, {"traceId": TRACE_B}]}
TRACE_A_DETAIL = {
    "traceId": TRACE_A,
    "spans": [
        {"name": "POST /v1/chat/completions", "startTime": "2026-09-28T10:00:00.000000000Z", "endTime": "2026-09-28T10:00:00.646000000Z"},
        {"name": "chat model-default", "startTime": "2026-09-28T10:00:00.010000000Z", "endTime": "2026-09-28T10:00:00.637000000Z"},
        {"name": "auth /v1/chat/completions", "startTime": "2026-09-28T10:00:00.001Z", "endTime": "2026-09-28T10:00:00.002Z"},
    ],
}
TRACE_B_DETAIL = {"traceId": TRACE_B, "spans": []}
BROKER_REFUSAL = {
    "status": "blocked",
    "code": "SECURITY_POLICY_BLOCKED",
    "rule": "gcp.api.path",
    "message": "cloudtrace.googleapis.com relays only the reads gcp.api.cloudtrace.traces-list, gcp.api.cloudtrace.traces-get; this path is none of them.",
}
GOOGLE_DENIED = {"error": {"code": 403, "message": "The caller does not have permission", "status": "PERMISSION_DENIED"}}


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        if isinstance(self._body, str):
            return json.loads(self._body)
        return self._body


class FakeHttp:
    """Answers each relayed GET from a table keyed by the URL without its query."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def get(self, url, *, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        if url not in self.answers:
            raise AssertionError(f"unexpected relayed GET {url}")
        answer = self.answers[url]
        return answer if isinstance(answer, FakeResponse) else FakeResponse(200, answer)


class PagedHttp(FakeHttp):
    """Serves each URL's answers in order, one per call, so a test can stage pages."""

    def get(self, url, *, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        if url not in self.answers or not self.answers[url]:
            raise AssertionError(f"unexpected relayed GET {url}")
        answer = self.answers[url].pop(0)
        return answer if isinstance(answer, FakeResponse) else FakeResponse(200, answer)


def run(main, argv, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv, **kwargs)
    return code, out.getvalue(), err.getvalue()


class BrokerSessionCase(unittest.TestCase):
    """A real ApiSession over a fake HTTP layer, with the caller token the broker expects."""

    def setUp(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        token_file = directory / "token"
        token_file.write_text("caller-token\n", encoding="utf-8")
        patcher = patch.dict(
            os.environ,
            {"CREDENTIAL_PROXY_URL": ENDPOINT + "/", "CREDENTIAL_PROXY_TOKEN_FILE": str(token_file)},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def session(self, answers):
        self.http = FakeHttp(answers)
        return credential_proxy_client.ApiSession(http=self.http)

    def paged_session(self, answers):
        self.http = PagedHttp(answers)
        return credential_proxy_client.ApiSession(http=self.http)

    def assert_every_call_is_a_relayed_read_the_policy_admits(self):
        self.assertTrue(self.http.calls, "the helper made no read")
        for call in self.http.calls:
            with self.subTest(url=call["url"]):
                self.assertEqual({"Authorization": "Bearer caller-token"}, call["headers"])
                self.assertEqual(google_api.RELAY_TIMEOUT_SECONDS, call["timeout"])
                self.assertTrue(call["url"].startswith(ENDPOINT + credential_proxy_client.API_RELAY_PREFIX))
                host, _, path = call["url"][len(ENDPOINT + credential_proxy_client.API_RELAY_PREFIX):].partition("/")
                query = urllib.parse.urlencode(call["params"] or {})
                decision = api_policy.evaluate(api_policy.API_READ_METHOD, host, path, query)
                self.assertTrue(decision.allowed, f"{host}/{path}: {decision.message}")


class AnalyzeTraceLatencyTest(BrokerSessionCase):
    def test_lists_then_reads_each_trace_through_the_relay_and_ranks_the_spans(self):
        session = self.session({
            RELAYED_TRACES: TRACE_LIST,
            f"{RELAYED_TRACES}/{TRACE_A}": TRACE_A_DETAIL,
            f"{RELAYED_TRACES}/{TRACE_B}": TRACE_B_DETAIL,
        })
        code, out, err = run(
            analyze_trace_latency.main, ["--project-id", PROJECT, "--hours", "2", "--limit", "2"], session=session
        )
        self.assertEqual(0, code, err)
        self.assertEqual("", err)
        self.assert_every_call_is_a_relayed_read_the_policy_admits()
        listing = self.http.calls[0]
        self.assertEqual(RELAYED_TRACES, listing["url"])
        self.assertEqual(2, listing["params"]["pageSize"])
        self.assertTrue(listing["params"]["startTime"].endswith("Z"))
        self.assertLess(listing["params"]["startTime"], listing["params"]["endTime"])
        self.assertEqual(
            [f"{RELAYED_TRACES}/{TRACE_A}", f"{RELAYED_TRACES}/{TRACE_B}"],
            [call["url"] for call in self.http.calls[1:]],
        )
        self.assertIn("Retrieving the last 2 traces...", out)
        self.assertIn(f"Trace ID: {TRACE_A}", out)
        self.assertIn("Total Duration: 0.646 seconds | Total Spans: 3", out)
        lines = [line for line in out.splitlines() if line.startswith("  - ")]
        self.assertTrue(lines[0].startswith("  - POST /v1/chat/completions"), lines)
        self.assertTrue(lines[-1].startswith("  - auth /v1/chat/completions"), lines)
        self.assertNotIn(f"Trace ID: {TRACE_B}", out)

    def test_an_empty_first_page_with_a_token_is_followed_not_reported_as_no_traces(self):
        # Cloud Trace answers an empty first page with a nextPageToken when the
        # newest bucket holds nothing; the traces are on the next page.
        session = self.paged_session({
            RELAYED_TRACES: [{"nextPageToken": "p2"}, {"traces": [{"traceId": TRACE_A}]}],
            f"{RELAYED_TRACES}/{TRACE_A}": [TRACE_A_DETAIL],
        })
        code, out, err = run(analyze_trace_latency.main, ["--project-id", PROJECT, "--limit", "1"], session=session)
        self.assertEqual(0, code, err)
        self.assertNotIn("No traces found", out)
        self.assertIn(f"Trace ID: {TRACE_A}", out)
        first, second = self.http.calls[0], self.http.calls[1]
        self.assertNotIn("pageToken", first["params"])
        self.assertEqual("p2", second["params"]["pageToken"])
        self.assertEqual(first["params"]["startTime"], second["params"]["startTime"])

    def test_listing_stops_once_the_limit_is_in_hand(self):
        session = self.paged_session({
            RELAYED_TRACES: [{"traces": [{"traceId": TRACE_A}], "nextPageToken": "more"}],
            f"{RELAYED_TRACES}/{TRACE_A}": [TRACE_A_DETAIL],
        })
        code, out, err = run(analyze_trace_latency.main, ["--project-id", PROJECT, "--limit", "1"], session=session)
        self.assertEqual(0, code, err)
        self.assertEqual([RELAYED_TRACES, f"{RELAYED_TRACES}/{TRACE_A}"], [c["url"] for c in self.http.calls])

    def test_fifty_empty_pages_with_a_token_are_a_failed_read_not_an_empty_window(self):
        # The cap fires with nothing in hand: the helper must not say "No
        # traces found" about a list it never saw the end of.
        session = self.paged_session({RELAYED_TRACES: [{"nextPageToken": "again"}] * (google_api.MAX_LIST_PAGES + 5)})
        code, out, err = run(analyze_trace_latency.main, ["--project-id", PROJECT, "--limit", "3"], session=session)
        self.assertEqual(google_api.EXIT_READ_FAILED, code)
        self.assertNotIn("No traces found", out)
        self.assertIn(f"stopped after {google_api.MAX_LIST_PAGES} pages", err)
        self.assertIn("nothing in hand", err)
        self.assertEqual(google_api.MAX_LIST_PAGES, len(self.http.calls))

    def test_an_empty_window_is_reported_and_exits_zero(self):
        session = self.session({RELAYED_TRACES: {}})
        code, out, err = run(analyze_trace_latency.main, ["--project-id", PROJECT], session=session)
        self.assertEqual(0, code)
        self.assertIn("No traces found in the specified window.", out)
        self.assertEqual(1, len(self.http.calls))
        self.assertEqual(analyze_trace_latency.DEFAULT_LIMIT, self.http.calls[0]["params"]["pageSize"])

    def test_a_limit_below_one_is_refused_before_any_read(self):
        # pageSize=0 makes Cloud Trace answer a default page, which the stop
        # rule would trim to nothing and the helper report as an empty window.
        for limit in ("0", "-1"):
            with self.subTest(limit=limit):
                session = self.session({RELAYED_TRACES: TRACE_LIST})
                err = io.StringIO()
                with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised:
                    analyze_trace_latency.main(["--project-id", PROJECT, "--limit", limit], session=session)
                self.assertEqual(ARGPARSE_USAGE_EXIT, raised.exception.code)
                self.assertIn(f"must be at least {google_api.MIN_LIMIT}", err.getvalue())
                self.assertEqual([], self.http.calls)

    def test_a_limit_of_one_is_the_smallest_the_parser_takes(self):
        self.assertEqual(1, analyze_trace_latency.parse_args(["--project-id", PROJECT, "--limit", "1"]).limit)

    def test_a_broker_refusal_names_the_rule_and_exits_one(self):
        session = self.session({RELAYED_TRACES: FakeResponse(403, BROKER_REFUSAL)})
        code, out, err = run(analyze_trace_latency.main, ["--project-id", PROJECT], session=session)
        self.assertEqual(google_api.EXIT_READ_FAILED, code)
        self.assertIn("gcp.api.path", err)
        self.assertIn("this path is none of them", err)
        self.assertNotIn("Trace ID:", out)

    def test_a_google_permission_denied_is_reported_with_its_status(self):
        session = self.session({RELAYED_TRACES: FakeResponse(403, GOOGLE_DENIED)})
        code, out, err = run(analyze_trace_latency.main, ["--project-id", PROJECT], session=session)
        self.assertEqual(google_api.EXIT_READ_FAILED, code)
        self.assertIn("PERMISSION_DENIED", err)
        self.assertIn("HTTP 403", err)

    def test_one_unreadable_trace_does_not_stop_the_others(self):
        session = self.session({
            RELAYED_TRACES: TRACE_LIST,
            f"{RELAYED_TRACES}/{TRACE_A}": FakeResponse(404, {"error": {"status": "NOT_FOUND", "message": "gone"}}),
            f"{RELAYED_TRACES}/{TRACE_B}": TRACE_A_DETAIL,
        })
        code, out, err = run(analyze_trace_latency.main, ["--project-id", PROJECT], session=session)
        self.assertEqual(0, code)
        self.assertIn(f"Error reading trace {TRACE_A}", err)
        self.assertIn("Trace ID:", out)

    def test_a_recorded_trace_parses_to_the_expected_durations(self):
        # 1.5s + 1s back to back: the trace spans exactly 2.5 seconds. A unit
        # slip would print 2500.000 or 0.003 here.
        detail = {"spans": [
            {"name": "model-call", "startTime": "2026-08-19T10:00:00Z", "endTime": "2026-08-19T10:00:01.500000Z"},
            {"name": "tool-call", "startTime": "2026-08-19T10:00:01.500000Z", "endTime": "2026-08-19T10:00:02.500000Z"},
        ]}
        session = self.session({RELAYED_TRACES: TRACE_LIST, f"{RELAYED_TRACES}/{TRACE_A}": detail,
                                f"{RELAYED_TRACES}/{TRACE_B}": TRACE_B_DETAIL})
        code, out, err = run(analyze_trace_latency.main, ["--project-id", PROJECT], session=session)
        self.assertEqual(0, code, err)
        self.assertIn("Total Duration: 2.500 seconds", out)
        self.assertIn("Total Spans: 2", out)
        self.assertLess(out.index("model-call"), out.index("tool-call"))
        self.assertIn("1.500s", out)
        self.assertIn("60.0%", out)

    def test_nanosecond_timestamps_do_not_distort_the_arithmetic(self):
        detail = {"spans": [
            {"name": "exact", "startTime": "2026-08-19T10:00:00.123456789Z", "endTime": "2026-08-19T10:00:02.123456789Z"},
        ]}
        session = self.session({RELAYED_TRACES: {"traces": [{"traceId": TRACE_A}]}, f"{RELAYED_TRACES}/{TRACE_A}": detail})
        code, out, err = run(analyze_trace_latency.main, ["--project-id", PROJECT], session=session)
        self.assertEqual(0, code, err)
        self.assertIn("Total Duration: 2.000 seconds", out)
        self.assertIn("2.000s", out)

    def test_spans_missing_timestamps_are_excluded_from_the_breakdown(self):
        detail = {"spans": [
            {"name": "good", "startTime": "2026-08-19T10:00:00Z", "endTime": "2026-08-19T10:00:01Z"},
            {"name": "no-times"},
        ]}
        session = self.session({RELAYED_TRACES: {"traces": [{"traceId": TRACE_A}]}, f"{RELAYED_TRACES}/{TRACE_A}": detail})
        code, out, err = run(analyze_trace_latency.main, ["--project-id", PROJECT], session=session)
        self.assertEqual(0, code, err)
        # The span count is honest about what arrived; the duration math only
        # uses what could be timed.
        self.assertIn("Total Spans: 2", out)
        self.assertIn("Total Duration: 1.000 seconds", out)
        self.assertNotIn("no-times", out)

    def test_parse_timestamp_reads_nanoseconds_and_offsets(self):
        parsed = analyze_trace_latency.parse_timestamp("2026-09-28T10:00:00.123456789Z")
        self.assertEqual(123456, parsed.microsecond)
        parsed = analyze_trace_latency.parse_timestamp("2026-09-28T10:00:00.5-07:00")
        self.assertEqual(500000, parsed.microsecond)
        self.assertEqual(-7 * 3600, parsed.utcoffset().total_seconds())


class FetchTracesTest(BrokerSessionCase):
    def test_lists_traces_through_the_relay_and_prints_the_body(self):
        session = self.session({RELAYED_TRACES: TRACE_LIST})
        code, out, err = run(fetch_traces.main, ["--project-id", PROJECT, "--hours", "48"], session=session)
        self.assertEqual(0, code, err)
        self.assert_every_call_is_a_relayed_read_the_policy_admits()
        self.assertEqual(fetch_traces.PAGE_SIZE, self.http.calls[0]["params"]["pageSize"])
        self.assertEqual(TRACE_LIST, json.loads(out))

    def test_an_empty_window_prints_an_empty_list_not_an_error(self):
        session = self.session({RELAYED_TRACES: {}})
        code, out, err = run(fetch_traces.main, ["--project-id", PROJECT], session=session)
        self.assertEqual(0, code, err)
        self.assertEqual({"traces": []}, json.loads(out))

    def test_pages_are_joined_up_to_the_page_size(self):
        pages = [{"traces": [{"traceId": f"{i:032x}"} for i in range(6)], "nextPageToken": "p2"},
                 {"traces": [{"traceId": f"{i:032x}"} for i in range(6, 12)], "nextPageToken": "p3"}]
        session = self.paged_session({RELAYED_TRACES: pages})
        code, out, err = run(fetch_traces.main, ["--project-id", PROJECT], session=session)
        self.assertEqual(0, code, err)
        self.assertEqual(fetch_traces.PAGE_SIZE, len(json.loads(out)["traces"]))
        self.assertEqual(2, len(self.http.calls))

    def test_a_list_that_never_runs_dry_stops_at_the_page_cap_and_says_so(self):
        session = self.paged_session({RELAYED_TRACES: [{"nextPageToken": "again"}] * (google_api.MAX_LIST_PAGES + 5)})
        code, out, err = run(fetch_traces.main, ["--project-id", PROJECT], session=session)
        self.assertEqual(google_api.EXIT_READ_FAILED, code)
        self.assertEqual("", out)
        self.assertIn(f"stopped after {google_api.MAX_LIST_PAGES} pages", err)
        self.assertEqual(google_api.MAX_LIST_PAGES, len(self.http.calls))

    def test_a_partial_list_at_the_page_cap_is_printed_with_a_note_on_stderr(self):
        # One trace on the first page, then empty token-bearing pages up to the
        # cap: what was read is printed, and stderr says it is not the whole list.
        pages = [{"traces": [{"traceId": TRACE_A}], "nextPageToken": "p2"}]
        pages += [{"nextPageToken": "again"}] * (google_api.MAX_LIST_PAGES + 5)
        session = self.paged_session({RELAYED_TRACES: pages})
        code, out, err = run(fetch_traces.main, ["--project-id", PROJECT], session=session)
        self.assertEqual(0, code, err)
        self.assertEqual([{"traceId": TRACE_A}], json.loads(out)["traces"])
        self.assertIn(f"warning: {google_api.TRACE_LIST_URL.format(project=PROJECT)}: stopped after {google_api.MAX_LIST_PAGES} pages", err)
        self.assertIn("1 item(s) returned are not the whole list", err)
        self.assertEqual(google_api.MAX_LIST_PAGES, len(self.http.calls))

    def test_a_refusal_exits_one_with_the_reason(self):
        session = self.session({RELAYED_TRACES: FakeResponse(403, BROKER_REFUSAL)})
        code, out, err = run(fetch_traces.main, ["--project-id", PROJECT], session=session)
        self.assertEqual(google_api.EXIT_READ_FAILED, code)
        self.assertIn("gcp.api.path", err)
        self.assertEqual("", out)

    def test_a_broker_that_cannot_be_reached_exits_one(self):
        class Unreachable:
            def get(self, url, **kwargs):
                raise ConnectionRefusedError("connection refused")

        session = credential_proxy_client.ApiSession(http=Unreachable())
        code, out, err = run(fetch_traces.main, ["--project-id", PROJECT], session=session)
        self.assertEqual(google_api.EXIT_READ_FAILED, code)
        self.assertIn("could not reach the credential broker", err)
        self.assertEqual("", out)


class CheckTokenUsageTest(BrokerSessionCase):
    SERIES = {
        "timeSeries": [
            {"points": [
                {"interval": {"endTime": "2026-09-28T10:00:00Z"}, "value": {"int64Value": "50"}},
                {"interval": {"endTime": "2026-09-28T09:00:00Z"}, "value": {"int64Value": "20"}},
                {"interval": {"endTime": "2026-09-28T08:00:00Z"}, "value": {"int64Value": "80"}},
            ]},
            {"points": [{"interval": {"endTime": "2026-09-28T10:00:00Z"}, "value": {"doubleValue": 5.0}}]},
        ]
    }

    def test_reads_the_three_counters_through_the_relay_and_sums_the_deltas(self):
        session = self.session({RELAYED_TIME_SERIES: self.SERIES})
        code, out, err = run(check_token_usage.main, ["--project-id", PROJECT], session=session)
        self.assertEqual(0, code, err)
        self.assert_every_call_is_a_relayed_read_the_policy_admits()
        filters = [call["params"]["filter"] for call in self.http.calls]
        self.assertEqual(
            [
                f'metric.type="{check_token_usage.INPUT_TOKENS_METRIC}"',
                f'metric.type="{check_token_usage.OUTPUT_TOKENS_METRIC}"',
                f'metric.type="{check_token_usage.CACHED_INPUT_TOKENS_METRIC}"',
            ],
            filters,
        )
        for call in self.http.calls:
            self.assertLess(call["params"]["interval.startTime"], call["params"]["interval.endTime"])
        # 80 -> 20 is a reset (counts 20), 20 -> 50 counts 30; the one-point series counts nothing.
        self.assertEqual({"input_tokens": 50, "output_tokens": 50, "cached_input_tokens": 50}, json.loads(out))

    def usage(self, input_series, output_series=None, cached_series=None):
        empty = {"timeSeries": []}
        by_filter = {
            f'metric.type="{check_token_usage.INPUT_TOKENS_METRIC}"': input_series,
            f'metric.type="{check_token_usage.OUTPUT_TOKENS_METRIC}"': output_series or empty,
            f'metric.type="{check_token_usage.CACHED_INPUT_TOKENS_METRIC}"': cached_series or empty,
        }

        class ByFilter(FakeHttp):
            def get(inner, url, *, params=None, headers=None, timeout=None):
                inner.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
                return FakeResponse(200, by_filter[params["filter"]])

        self.http = ByFilter({})
        session = credential_proxy_client.ApiSession(http=self.http)
        code, out, err = run(check_token_usage.main, ["--project-id", PROJECT], session=session)
        self.assertEqual(0, code, err)
        return json.loads(out)

    @staticmethod
    def point(end_time, value):
        return {"interval": {"endTime": end_time}, "value": value}

    def test_newest_first_points_are_sorted_before_diffing(self):
        # The API returns newest first; without the sort 100 -> 350 reads as a
        # reset and the delta comes out as 100.
        result = self.usage({"timeSeries": [{"points": [
            self.point("2026-08-19T10:05:00Z", {"doubleValue": 350.0}),
            self.point("2026-08-19T10:00:00Z", {"doubleValue": 100.0}),
        ]}]})
        self.assertEqual({"input_tokens": 250, "output_tokens": 0, "cached_input_tokens": 0}, result)

    def test_int64_values_arrive_as_strings_and_still_count(self):
        result = self.usage({"timeSeries": []}, output_series={"timeSeries": [{"points": [
            self.point("2026-08-19T10:00:00Z", {"int64Value": "1000"}),
            self.point("2026-08-19T10:05:00Z", {"int64Value": "4000"}),
        ]}]})
        self.assertEqual(3000, result["output_tokens"])

    def test_a_counter_reset_adds_the_post_reset_value_not_a_negative(self):
        result = self.usage({"timeSeries": [{"points": [
            self.point("2026-08-19T10:00:00Z", {"doubleValue": 500.0}),
            self.point("2026-08-19T10:05:00Z", {"doubleValue": 30.0}),
        ]}]})
        self.assertEqual(30, result["input_tokens"])

    def test_deltas_sum_across_pods(self):
        result = self.usage({"timeSeries": [
            {"points": [self.point("2026-08-19T10:00:00Z", {"doubleValue": 0.0}),
                        self.point("2026-08-19T10:05:00Z", {"doubleValue": 100.0})]},
            {"points": [self.point("2026-08-19T10:00:00Z", {"doubleValue": 0.0}),
                        self.point("2026-08-19T10:05:00Z", {"doubleValue": 42.0})]},
        ]})
        self.assertEqual(142, result["input_tokens"])

    def test_no_data_reports_zeroes_in_the_documented_shape(self):
        self.assertEqual(
            {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0},
            self.usage({"timeSeries": []}),
        )

    def test_a_refusal_is_not_reported_as_zero_tokens(self):
        session = self.session({RELAYED_TIME_SERIES: FakeResponse(403, GOOGLE_DENIED)})
        code, out, err = run(check_token_usage.main, ["--project-id", PROJECT], session=session)
        self.assertEqual(google_api.EXIT_READ_FAILED, code)
        self.assertEqual("", out)
        self.assertIn("PERMISSION_DENIED", err)


class GetMetricDescriptorsTest(BrokerSessionCase):
    def test_lists_descriptors_through_the_relay_and_keeps_the_litellm_ones(self):
        session = self.session({RELAYED_DESCRIPTORS: {"metricDescriptors": [
            {"type": "prometheus.googleapis.com/litellm_input_tokens_metric_total/counter"},
            {"type": "kubernetes.io/container/cpu/core_usage_time"},
            {"name": "no type"},
        ]}})
        code, out, err = run(get_metric_descriptors.main, ["--project-id", PROJECT], session=session)
        self.assertEqual(0, code, err)
        self.assert_every_call_is_a_relayed_read_the_policy_admits()
        self.assertEqual(RELAYED_DESCRIPTORS, self.http.calls[0]["url"])
        # Filtered on the server and paged: the whole descriptor list runs past
        # the relay's response cap.
        self.assertEqual('metric.type = has_substring("litellm")', self.http.calls[0]["params"]["filter"])
        self.assertEqual(get_metric_descriptors.PAGE_SIZE, self.http.calls[0]["params"]["pageSize"])
        self.assertEqual(["prometheus.googleapis.com/litellm_input_tokens_metric_total/counter"], json.loads(out))

    def test_descriptor_pages_are_joined(self):
        session = self.paged_session({RELAYED_DESCRIPTORS: [
            {"metricDescriptors": [{"type": "prometheus.googleapis.com/litellm_a/counter"}], "nextPageToken": "p2"},
            {"metricDescriptors": [{"type": "prometheus.googleapis.com/litellm_b/counter"}]},
        ]})
        code, out, err = run(get_metric_descriptors.main, ["--project-id", PROJECT], session=session)
        self.assertEqual(0, code, err)
        self.assertEqual("p2", self.http.calls[1]["params"]["pageToken"])
        self.assertEqual(["prometheus.googleapis.com/litellm_a/counter", "prometheus.googleapis.com/litellm_b/counter"], json.loads(out))

    def test_an_empty_response_reports_an_empty_list(self):
        session = self.session({RELAYED_DESCRIPTORS: {}})
        code, out, err = run(get_metric_descriptors.main, ["--project-id", PROJECT], session=session)
        self.assertEqual(0, code, err)
        self.assertEqual([], json.loads(out))

    def test_a_refusal_exits_one_rather_than_reporting_no_metrics(self):
        session = self.session({RELAYED_DESCRIPTORS: FakeResponse(403, GOOGLE_DENIED)})
        code, out, err = run(get_metric_descriptors.main, ["--project-id", PROJECT], session=session)
        self.assertEqual(google_api.EXIT_READ_FAILED, code)
        self.assertIn("PERMISSION_DENIED", err)
        self.assertEqual("", out)


class GoogleApiTest(BrokerSessionCase):
    def test_open_session_without_a_broker_url_is_a_relay_error(self):
        with patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": ""}):
            with self.assertRaises(google_api.RelayError) as raised:
                google_api.open_session()
        self.assertIn("CREDENTIAL_PROXY_URL", str(raised.exception))

    def test_a_missing_caller_token_is_a_relay_error_not_a_traceback(self):
        session = self.session({RELAYED_TRACES: TRACE_LIST})
        with patch.dict(os.environ, {"CREDENTIAL_PROXY_TOKEN_FILE": "/nonexistent/token"}):
            with self.assertRaises(google_api.RelayError):
                google_api.get_json(session, f"https://cloudtrace.googleapis.com/v1/projects/{PROJECT}/traces")

    def test_a_non_json_success_body_is_a_relay_error(self):
        session = self.session({RELAYED_TRACES: FakeResponse(200, "<html>")})
        with self.assertRaises(google_api.RelayError):
            google_api.get_json(session, f"https://cloudtrace.googleapis.com/v1/projects/{PROJECT}/traces")

    def test_get_paginated_refuses_a_limit_below_one_without_reading(self):
        # A default page trimmed by items[:0] would read as an empty list.
        session = self.session({RELAYED_TRACES: TRACE_LIST})
        url = google_api.TRACE_LIST_URL.format(project=PROJECT)
        for limit in (0, -1):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                google_api.get_paginated(session, url, params={}, items_key="traces", limit=limit)
        self.assertEqual([], self.http.calls)
        self.assertEqual(2, len(google_api.get_paginated(session, url, params={}, items_key="traces", limit=None)))


class GetChatUsersTest(unittest.TestCase):
    ENTRIES = [
        {"textPayload": "Logging incoming GChat event: User=ana@example.com, Session=1"},
        {"jsonPayload": {"log": "Logging incoming GChat event: User=ana@example.com, Session=2"}},
        {"jsonPayload": {"message": "Logging incoming GChat event: User=bo@example.com, Session=3"}},
        {"textPayload": "nothing here"},
    ]

    def completed(self, returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)

    def test_reads_through_the_brokered_gcloud_and_counts_users(self):
        with patch.object(get_chat_users.subprocess, "run", return_value=self.completed(stdout=json.dumps(self.ENTRIES))) as run_mock:
            code, out, err = run(get_chat_users.main, ["--project-id", PROJECT, "--hours", "72"])
        self.assertEqual(0, code, err)
        argv = run_mock.call_args.args[0]
        self.assertEqual(
            [
                "gcloud", "logging", "read", get_chat_users.LOG_FILTER,
                f"--project={PROJECT}", "--limit=1000", "--format=json", "--freshness=72h",
            ],
            argv,
        )
        self.assertTrue(run_mock.call_args.kwargs["capture_output"])
        decision = command_policy.evaluate(argv)
        self.assertTrue(decision.allowed, decision.message)
        report = json.loads(out)
        self.assertEqual({"ana@example.com": 2, "bo@example.com": 1}, report["active_chat_users"])
        self.assertEqual(72, report["time_window_hours"])

    def test_a_refused_or_failed_gcloud_exits_one_with_its_stderr(self):
        refusal = self.completed(returncode=1, stderr="Command blocked by security policy (gcloud.read-only)")
        with patch.object(get_chat_users.subprocess, "run", return_value=refusal):
            code, out, err = run(get_chat_users.main, ["--project-id", PROJECT])
        self.assertEqual(get_chat_users.EXIT_READ_FAILED, code)
        self.assertIn("security policy", err)
        self.assertEqual("", out)

    def test_a_truncated_read_is_a_failure_that_names_the_cap_not_a_json_error(self):
        # The shim writes the cut body, notes the truncation on stderr and exits
        # as gcloud did (0); the cut JSON must not be reported as malformed.
        cut = json.dumps(self.ENTRIES)[:-40]
        truncated = self.completed(returncode=0, stdout=cut, stderr="credential proxy output truncated\n")
        with patch.object(get_chat_users.subprocess, "run", return_value=truncated):
            code, out, err = run(get_chat_users.main, ["--project-id", PROJECT])
        self.assertEqual(get_chat_users.EXIT_READ_FAILED, code)
        self.assertEqual("", out)
        self.assertIn("credential proxy output truncated", err)
        self.assertIn("--hours", err)
        self.assertNotIn("did not return JSON", err)

    def test_the_truncation_note_is_the_line_the_shim_prints(self):
        shim = Path(credential_proxy_client.__file__).read_text(encoding="utf-8")
        self.assertIn(f'"{get_chat_users.SHIM_TRUNCATION_NOTE}"', shim)

    def test_malformed_json_on_exit_zero_reports_stderr_with_the_parse_error(self):
        broken = self.completed(returncode=0, stdout="[{", stderr="WARNING: something gcloud said")
        with patch.object(get_chat_users.subprocess, "run", return_value=broken):
            code, out, err = run(get_chat_users.main, ["--project-id", PROJECT])
        self.assertEqual(get_chat_users.EXIT_READ_FAILED, code)
        self.assertIn("did not return JSON", err)
        self.assertIn("something gcloud said", err)

    def test_users_are_sorted_by_email_and_unmarked_entries_are_not_counted(self):
        entries = [
            {"textPayload": "Logging incoming GChat event User=zoe@example.com space=..."},
            {"textPayload": "Logging incoming GChat event with no user field"},
            {"textPayload": "Logging incoming GChat event User=ann@example.com space=..."},
        ]
        with patch.object(get_chat_users.subprocess, "run", return_value=self.completed(stdout=json.dumps(entries))):
            code, out, err = run(get_chat_users.main, ["--project-id", PROJECT])
        self.assertEqual(0, code, err)
        self.assertEqual(["ann@example.com", "zoe@example.com"], list(json.loads(out)["active_chat_users"]))

    def test_an_empty_read_is_an_empty_count(self):
        with patch.object(get_chat_users.subprocess, "run", return_value=self.completed(stdout="[]")):
            code, out, err = run(get_chat_users.main, ["--project-id", PROJECT])
        self.assertEqual(0, code)
        self.assertEqual({}, json.loads(out)["active_chat_users"])


class NoTokenInAnyHelperTest(unittest.TestCase):
    """The property the port exists for, held on the source: nothing here fetches or holds a token."""

    def test_no_helper_names_a_token_fetch_a_bearer_header_or_a_direct_google_call(self):
        for name in HELPERS:
            source = (HERE / name).read_text(encoding="utf-8")
            with self.subTest(helper=name):
                self.assertNotIn("print-access-token", source)
                self.assertNotIn("Authorization", source)
                self.assertNotIn("Bearer", source)
                self.assertNotIn("urllib.request", source)
                self.assertNotIn("metadata.google.internal", source)

    def test_the_relay_helpers_read_through_the_shared_session(self):
        for name in HELPERS:
            if name in ("get_chat_users.py", "google_api.py"):
                continue
            source = (HERE / name).read_text(encoding="utf-8")
            with self.subTest(helper=name):
                self.assertIn("google_api.open_session()", source)
                # Every read goes through one of the module's readers.
                self.assertTrue(
                    any(f"google_api.{reader}(" in source for reader in ("get_json", "get_paginated", "list_traces")),
                    f"{name} reads through none of google_api's readers",
                )
                # The Trace and Monitoring endpoints and the window arithmetic
                # are written once, in google_api.py or in the one helper that
                # reads that endpoint; no helper re-declares a Trace URL.
                self.assertNotIn("cloudtrace.googleapis.com", source)
                self.assertNotIn("strftime", source)

    def test_the_session_is_the_broker_client(self):
        source = (HERE / "google_api.py").read_text(encoding="utf-8")
        self.assertIn("credential_proxy_client.ApiSession()", source)


if __name__ == "__main__":
    unittest.main()
