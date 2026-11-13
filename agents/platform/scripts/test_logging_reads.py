#!/usr/bin/env python3
"""Tests for logging_reads: the relay read that stands in for `gcloud logging read`.

Run:  python3 agents/platform/scripts/test_logging_reads.py
"""

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import api_policy
import logging_reads

# The body `gcloud logging read FILTER --project devops-project --freshness 24h
# --limit 1000 --format json` sent, read from its own HTTP log (`--log-http`):
# the bound first, the default order, one project, a page of up to 1000.
GCLOUD_FILTER = 'log_id("container.googleapis.com/cluster-autoscaler-visibility") AND resource.labels.cluster_name="c"'
GCLOUD_BODY = {
    "filter": 'timestamp>="2026-11-12T21:23:25.162722Z" AND ' + GCLOUD_FILTER,
    "orderBy": "timestamp desc",
    "pageSize": 1000,
    "resourceNames": ["projects/devops-project"],
}
GCLOUD_NOW = datetime(2026, 11, 13, 21, 23, 25, 162722, tzinfo=timezone.utc)


class Response:
    def __init__(self, status_code=200, payload=None, raises=None):
        self.status_code = status_code
        self._payload = payload
        self._raises = raises

    def json(self):
        if self._raises is not None:
            raise self._raises
        return self._payload


class Session:
    """Answers each POST with the next scripted response and records what was sent."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.sent = []

    def post(self, url, *, json=None, timeout=None):  # noqa: A002 -- the requests keyword
        self.sent.append({"url": url, "json": json, "timeout": timeout})
        answer = self.responses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def entries(*ids):
    return [{"insertId": i, "timestamp": "2026-11-13T00:00:00Z"} for i in ids]


class ReadJsonTest(unittest.TestCase):
    def read(self, session, limit=1000, timeout_s=60, **kwargs):
        return logging_reads.read_json(
            "devops-project", "severity>=ERROR", limit, timeout_s=timeout_s, session=session, **kwargs
        )

    def test_one_page_comes_back_as_the_json_list_gcloud_prints(self):
        session = Session(Response(payload={"entries": entries("a", "b")}))
        self.assertEqual(entries("a", "b"), json.loads(self.read(session)))
        [sent] = session.sent
        self.assertEqual("https://logging.googleapis.com/v2/entries:list", sent["url"])
        self.assertEqual(
            {
                "resourceNames": ["projects/devops-project"],
                "filter": "severity>=ERROR",
                "orderBy": "timestamp desc",
                "pageSize": logging_reads.PAGE_SIZE,
            },
            sent["json"],
        )

    def test_nothing_matched_is_an_empty_list_not_none(self):
        self.assertEqual("[]", self.read(Session(Response(payload={}))))

    def test_the_pages_are_followed_by_token_up_to_the_limit(self):
        session = Session(
            Response(payload={"entries": entries("a", "b"), "nextPageToken": "t1"}),
            Response(payload={"entries": [], "nextPageToken": "t2"}),
            Response(payload={"entries": entries("c")}),
        )
        self.assertEqual(entries("a", "b", "c"), json.loads(self.read(session)))
        self.assertEqual([None, "t1", "t2"], [s["json"].get("pageToken") for s in session.sent])

    def test_the_read_stops_at_the_limit_and_asks_for_no_more_than_is_left(self):
        session = Session(
            Response(payload={"entries": entries("a", "b"), "nextPageToken": "t1"}),
            Response(payload={"entries": entries("c", "d"), "nextPageToken": "t2"}),
        )
        self.assertEqual(entries("a", "b", "c"), json.loads(self.read(session, limit=3)))
        self.assertEqual([3, 1], [s["json"]["pageSize"] for s in session.sent])

    def test_the_body_is_the_body_gcloud_sends(self):
        session = Session(Response(payload={}))
        filter_text = logging_reads.freshness_filter(GCLOUD_FILTER, timedelta(hours=24), GCLOUD_NOW)
        logging_reads.read_json("devops-project", filter_text, 1000, timeout_s=60, session=session)
        self.assertEqual(GCLOUD_BODY, session.sent[0]["json"])

    def test_a_later_page_asks_for_what_is_left_as_gcloud_does(self):
        # gcloud's second request for the same read carried pageSize 936 after a page of 64.
        session = Session(
            Response(payload={"entries": entries(*map(str, range(64))), "nextPageToken": "t1"}),
            Response(payload={}),
        )
        self.read(session)
        self.assertEqual([1000, 936], [s["json"]["pageSize"] for s in session.sent])

    def test_every_body_it_sends_passes_the_brokers_body_check(self):
        session = Session(
            Response(payload={"entries": entries("a"), "nextPageToken": "t1"}),
            Response(payload={"entries": entries("b")}),
        )
        self.read(session)
        for sent in session.sent:
            decision = api_policy.evaluate_body(api_policy.LOGGING_ENTRIES_LIST_RULE, sent["json"])
            self.assertTrue(decision.allowed, decision.message)

    def test_anything_but_a_whole_answer_is_none_so_the_caller_runs_gcloud(self):
        cases = {
            "a refusal from an older broker": [Response(403, {"rule": "gcp.api.method"})],
            "the relay is off": [Response(503, {"code": "API_RELAY_DISABLED"})],
            "an upstream error": [Response(500, {"error": {"code": 500}})],
            "a body that is not JSON": [Response(raises=ValueError("not json"))],
            "an answer that is not an object": [Response(payload=[])],
            "entries that is not a list": [Response(payload={"entries": "x"})],
            "an entry that is not an object": [Response(payload={"entries": ["x"]})],
            "a token that is not a string": [Response(payload={"entries": entries("a"), "nextPageToken": 7})],
            "a failure on a later page": [
                Response(payload={"entries": entries("a"), "nextPageToken": "t1"}),
                Response(502, {"code": "UPSTREAM_RESPONSE_TOO_LARGE"}),
            ],
            "a connection error": [ConnectionError("refused")],
        }
        for name, responses in cases.items():
            with self.subTest(name):
                self.assertIsNone(self.read(Session(*responses)))

    def test_a_spent_budget_is_a_timed_out_read_and_no_partial_list(self):
        ticks = iter([0.0, 1.0, 200.0])
        session = Session(Response(payload={"entries": entries("a"), "nextPageToken": "t1"}))
        with self.assertRaises(logging_reads.ReadTimedOut) as timed_out:
            self.read(session, timeout_s=120, clock=lambda: next(ticks))
        self.assertEqual("timed out after 120 seconds", str(timed_out.exception))
        self.assertEqual(1, len(session.sent))
        self.assertEqual(119.0, session.sent[0]["timeout"])

    def test_a_request_cut_off_by_the_budget_is_a_timed_out_read(self):
        # The client's own timeout fires at the end of the budget: not "no relay".
        ticks = iter([0.0, 1.0, 120.5])
        with self.assertRaises(logging_reads.ReadTimedOut):
            self.read(Session(TimeoutError("read timed out")), timeout_s=120, clock=lambda: next(ticks))

    def test_a_fault_with_budget_left_is_none(self):
        ticks = iter([0.0, 1.0, 2.0])
        self.assertIsNone(self.read(Session(ConnectionError("refused")), timeout_s=120, clock=lambda: next(ticks)))

    def test_without_a_broker_nothing_is_sent(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(logging_reads.read_json("devops-project", "x", 10, timeout_s=60))

    def test_a_broker_the_client_cannot_use_is_none(self):
        # The variable is set, but the session cannot be built or sent on (no
        # caller token here): the fault is swallowed and the caller runs gcloud.
        with mock.patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:9"}, clear=True), \
                mock.patch.object(logging_reads, "_session", None):
            self.assertIsNone(logging_reads.read_json("devops-project", "x", 10, timeout_s=5))

    def test_one_session_serves_every_read_of_the_process(self):
        built = []

        class Client:
            @staticmethod
            def ApiSession():  # noqa: N802 -- the client module's class
                built.append(Session(Response(payload={}), Response(payload={})))
                return built[-1]

        with mock.patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": "http://broker"}), \
                mock.patch.dict("sys.modules", {"credential_proxy_client": Client}), \
                mock.patch.object(logging_reads, "_session", None):
            self.assertEqual("[]", logging_reads.read_json("devops-project", "x", 10, timeout_s=60))
            self.assertEqual("[]", logging_reads.read_json("devops-project", "x", 10, timeout_s=60))
        self.assertEqual(1, len(built))
        self.assertEqual(2, len(built[0].sent))


class FreshnessFilterTest(unittest.TestCase):
    def test_the_bound_goes_first_in_gclouds_format(self):
        self.assertEqual(GCLOUD_BODY["filter"], logging_reads.freshness_filter(GCLOUD_FILTER, timedelta(hours=24), GCLOUD_NOW))

    def test_a_filter_that_already_names_timestamp_gets_no_bound_as_with_gcloud(self):
        for filter_text in ('timestamp>="2026-11-01T00:00:00Z" AND severity>=ERROR', 'resource.labels.cluster_name="timestamp-lab"'):
            with self.subTest(filter_text=filter_text):
                self.assertEqual(filter_text, logging_reads.freshness_filter(filter_text, timedelta(hours=24), GCLOUD_NOW))

    def test_without_a_given_now_the_bound_is_from_the_utc_clock(self):
        class FixedClock(datetime):
            @classmethod
            def now(cls, tz=None):
                assert tz is timezone.utc, "the bound is computed in UTC"
                return GCLOUD_NOW

        with mock.patch.object(logging_reads, "datetime", FixedClock):
            self.assertEqual(GCLOUD_BODY["filter"], logging_reads.freshness_filter(GCLOUD_FILTER, timedelta(hours=24)))


if __name__ == "__main__":
    unittest.main()
