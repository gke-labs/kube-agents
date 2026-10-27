"""broker_admission: a fleet sweep's width, from what the credential broker admits.

Run:
  python3 -m unittest discover -s agents/platform/scripts -p 'test_broker_admission.py' -v
"""

from __future__ import annotations

import http.client
import io
import json
import os
import unittest
import urllib.error
from unittest import mock

import broker_admission
import credential_proxy_client


class WidthForTest(unittest.TestCase):
    def test_the_width_for_each_admitted_count(self):
        # Two slots of headroom from six up; at most today's four below that, so
        # an Autopilot install without bursting (4) sweeps no slower than before.
        for admitted, width in ((8, 6), (7, 5), (6, 4), (5, 4), (4, 4), (3, 3), (2, 2), (1, 1), (16, 14)):
            with self.subTest(admitted=admitted):
                self.assertEqual(width, broker_admission.width_for(admitted))

    def test_a_raised_session_cap_never_takes_the_width_below_four(self):
        # An operator can raise the session cap; the width still never drops
        # below today's four.
        for admitted, headroom in ((8, 6), (8, 8), (6, 10), (16, 15)):
            with self.subTest(admitted=admitted, headroom=headroom):
                self.assertEqual(4, broker_admission.width_for(admitted, headroom=headroom))


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class ReadReportTest(unittest.TestCase):
    ENDPOINT = "http://credential-proxy:8080"

    def _reading(self, body=None, error=None):
        def opener(request, timeout=None):
            self.seen.append((request, timeout))
            if error is not None:
                raise error
            return _Response(json.dumps(body).encode())

        self.seen = []
        return mock.patch.object(broker_admission._OPENER, "open", side_effect=opener)

    def test_it_reads_the_admitted_count_from_the_route(self):
        with mock.patch.object(credential_proxy_client, "authorization_headers", return_value={"Authorization": "Bearer t"}), \
                self._reading({"slot_cap": 8, "budget_admits": 9, "admitted": 8, "session_cap": 2}):
            self.assertEqual((8, 2), broker_admission.read_report(self.ENDPOINT))
        request, timeout = self.seen[0]
        self.assertEqual(self.ENDPOINT + "/v1/exec/admission", request.full_url)
        self.assertEqual("Bearer t", request.get_header("Authorization"))
        # Bounded end to end, unlike the broker client's exec opener.
        self.assertEqual(broker_admission.ADMISSION_TIMEOUT_S, timeout)

    def test_a_missing_or_bad_session_cap_reads_as_the_default(self):
        for name, body in {"missing": {"admitted": 8}, "zero": {"admitted": 8, "session_cap": 0}, "text": {"admitted": 8, "session_cap": "2"}}.items():
            with self.subTest(case=name), mock.patch.object(credential_proxy_client, "authorization_headers", return_value={}), \
                    self._reading(body):
                self.assertEqual((8, broker_admission.CHAT_HEADROOM), broker_admission.read_report(self.ENDPOINT))

    def test_every_failure_reads_as_unknown(self):
        cases = {
            "an older broker's 404": dict(error=urllib.error.HTTPError(self.ENDPOINT, 404, "Not Found", {}, None)),
            "a refusal": dict(error=urllib.error.HTTPError(self.ENDPOINT, 403, "Forbidden", {}, None)),
            "no connection": dict(error=urllib.error.URLError("refused")),
            "a body cut short": dict(error=http.client.IncompleteRead(b"{")),
            "a mangled status line": dict(error=http.client.BadStatusLine("garbage")),
            "a read that times out": dict(error=TimeoutError("timed out")),
            "not a count": dict(body={"admitted": "eight"}),
            "a boolean": dict(body={"admitted": True}),
            "zero": dict(body={"admitted": 0}),
            "no field": dict(body={"slot_cap": 8}),
            "not an object": dict(body=[8]),
        }
        for name, case in cases.items():
            with self.subTest(case=name), mock.patch.object(credential_proxy_client, "authorization_headers", return_value={}), \
                    self._reading(**case):
                self.assertIsNone(broker_admission.read_report(self.ENDPOINT))

    def test_an_unreadable_token_reads_as_unknown(self):
        with mock.patch.object(
            credential_proxy_client, "authorization_headers",
            side_effect=credential_proxy_client.TokenUnavailable("/token: missing"),
        ):
            self.assertIsNone(broker_admission.read_report(self.ENDPOINT))

    def test_no_broker_url_reads_as_unknown_without_a_request(self):
        with mock.patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": ""}), self._reading({"admitted": 8}):
            self.assertIsNone(broker_admission.read_report())
        self.assertEqual([], self.seen)


class SweepWidthTest(unittest.TestCase):
    def setUp(self):
        broker_admission._cached_width = None
        self.addCleanup(setattr, broker_admission, "_cached_width", None)

    def test_it_is_the_width_for_what_the_broker_admits(self):
        with mock.patch.object(broker_admission, "read_report", return_value=(8, 2)):
            self.assertEqual(6, broker_admission.sweep_width())

    def test_it_keeps_the_session_cap_the_broker_reports(self):
        with mock.patch.object(broker_admission, "read_report", return_value=(8, 3)):
            self.assertEqual(5, broker_admission.sweep_width())

    def test_it_falls_back_to_four_when_the_broker_cannot_say(self):
        with mock.patch.object(broker_admission, "read_report", return_value=None):
            self.assertEqual(broker_admission.FALLBACK_WIDTH, broker_admission.sweep_width())
        self.assertEqual(4, broker_admission.FALLBACK_WIDTH)

    def test_it_asks_once_per_process(self):
        with mock.patch.object(broker_admission, "read_report", return_value=(8, 2)) as read:
            broker_admission.sweep_width()
            broker_admission.sweep_width()
        self.assertEqual(1, read.call_count)


if __name__ == "__main__":
    unittest.main()
