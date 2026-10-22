"""The filing client calls the broker once per confirmed text and never hides a failure.

The broker (agents/platform/scripts/feedback_broker.py) owns the form's wire
path; this script owns the one thing the broker cannot: the memory that this
workspace already asked, kept in a marker written before the call. These tests
hold the marker's contract and the shape of what the agent reads back.

Run:
  python3 -m unittest discover -s agents/platform/skills/kube-agents-feedback/scripts \
      -p 'test_file_feedback.py' -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import file_feedback  # noqa: E402

PAYLOAD = {
    "title": "credential proxy dies silently",
    "kind": "Bug",
    "happened": "the sidecar crash-loops and nothing surfaces it",
    "user_confirmed": True,
}
ISSUE_URL = "https://github.com/gke-labs/kube-agents/issues/9999"
FALLBACK = {
    "tracker": "https://github.com/gke-labs/kube-agents/issues",
    "form": "https://gke-labs.github.io/kube-agents/feedback",
}
SUBMITTED = {"status": "SUBMITTED", "issue_url": ISSUE_URL, "fallback": FALLBACK}
ALREADY = {"status": "ALREADY_SUBMITTED", "issue_url": ISSUE_URL, "fallback": FALLBACK}
BLOCKED_400 = {"status": "BLOCKED", "reason": "kind 'Complaint' is not one of Bug, ...", "fallback": FALLBACK}
BROKER = "http://127.0.0.1:8765"


class FileFeedbackTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name)
        # The token file is pinned empty: the runner's own, if the shell sourced
        # a pod's environment, must neither be read nor make the tests fail.
        env = mock.patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": BROKER, "CREDENTIAL_PROXY_TOKEN_FILE": ""})
        env.start()
        self.addCleanup(env.stop)

    def _run(self, payload: dict, broker) -> tuple[int, dict, list]:
        path = self.state_dir / "payload.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        calls: list[dict] = []

        def post(request):
            calls.append(json.loads(request.data))
            if isinstance(broker, Exception):
                raise broker
            return broker

        out = io.StringIO()
        with mock.patch.object(file_feedback, "_post_to_broker", side_effect=post):
            with contextlib.redirect_stdout(out):
                code = file_feedback.submit(path, self.state_dir)
        return code, json.loads(out.getvalue().strip()), calls

    def _markers(self) -> list[Path]:
        return sorted(self.state_dir.glob(f"{file_feedback.MARKER_PREFIX}*"))

    def test_first_submit_writes_the_marker_and_prints_the_brokers_answer(self) -> None:
        code, printed, calls = self._run(PAYLOAD, (200, SUBMITTED))
        self.assertEqual((0, SUBMITTED), (code, printed))
        self.assertEqual(1, len(calls))
        self.assertNotIn("recheck_only", calls[0])
        (marker,) = self._markers()
        state = json.loads(marker.read_text())
        self.assertEqual((ISSUE_URL, "SUBMITTED"), (state["issue_url"], state["status"]))

    def test_second_submit_of_the_same_text_does_not_call_the_broker(self) -> None:
        self._run(PAYLOAD, (200, SUBMITTED))
        code, printed, calls = self._run(PAYLOAD, (200, SUBMITTED))
        self.assertEqual(0, code)
        self.assertEqual(("ALREADY_SUBMITTED", ISSUE_URL), (printed["status"], printed["issue_url"]))
        self.assertEqual(FALLBACK, printed["fallback"])
        self.assertEqual([], calls, "the duplicate-filing bug, exactly")

    def test_lost_answer_rechecks_only(self) -> None:
        # The first call died after the marker was written; the rerun asks
        # the broker to read the tracker and never to POST.
        with self.assertRaises(SystemExit):
            # A broker that hangs up mid-answer is not a BLOCKED (the POST may
            # have gone out): the marker stays and the script exits non-zero.
            self._run(PAYLOAD, file_feedback.BrokerAnswerLost("connection reset"))
        self.assertEqual(1, len(self._markers()), "the marker outlives a lost answer")
        code, printed, calls = self._run(PAYLOAD, (200, ALREADY))
        self.assertEqual((0, "ALREADY_SUBMITTED"), (code, printed["status"]))
        self.assertEqual([True], [c.get("recheck_only") for c in calls])

    def test_a_broker_error_is_blocked_with_the_links_and_leaves_no_marker(self) -> None:
        for broker in ((503, {}), (502, {"error": "bad gateway"}), urllib.error.URLError("refused")):
            with self.subTest(broker=broker):
                code, printed, _ = self._run(PAYLOAD, broker)
                self.assertEqual((1, "BLOCKED"), (code, printed["status"]))
                self.assertEqual(FALLBACK, printed["fallback"])
                self.assertEqual([], self._markers(), "nothing was sent, so nothing is remembered")

    def test_a_400_is_printed_as_received_and_exits_1(self) -> None:
        code, printed, _ = self._run(dict(PAYLOAD, kind="Complaint"), (400, BLOCKED_400))
        self.assertEqual((1, BLOCKED_400), (code, printed))
        self.assertEqual([], self._markers())

    def test_a_missing_token_is_blocked_and_leaves_no_marker(self) -> None:
        path = self.state_dir / "payload.json"
        path.write_text(json.dumps(PAYLOAD), encoding="utf-8")
        out = io.StringIO()
        with mock.patch.object(
            file_feedback.credential_proxy_client, "authorization_headers",
            side_effect=file_feedback.credential_proxy_client.TokenUnavailable("empty"),
        ):
            with contextlib.redirect_stdout(out):
                code = file_feedback.submit(path, self.state_dir)
        printed = json.loads(out.getvalue().strip())
        self.assertEqual((1, "BLOCKED"), (code, printed["status"]))
        self.assertIn("token", printed["reason"])
        self.assertEqual(FALLBACK, printed["fallback"])
        self.assertEqual([], self._markers())

    def test_submitted_without_a_url_is_rechecked_on_the_next_run(self) -> None:
        pending = {"status": "SUBMITTED", "issue_url": None, "detail": "not on the tracker yet", "fallback": FALLBACK}
        code, printed, _ = self._run(PAYLOAD, (200, pending))
        self.assertEqual((0, pending), (code, printed))
        code, printed, calls = self._run(PAYLOAD, (200, ALREADY))
        self.assertEqual((0, "ALREADY_SUBMITTED"), (code, printed["status"]))
        self.assertEqual([True], [c.get("recheck_only") for c in calls])
        (marker,) = self._markers()
        self.assertEqual(ISSUE_URL, json.loads(marker.read_text())["issue_url"])

    def test_a_transport_failure_on_the_recheck_keeps_the_marker_and_is_not_blocked(self) -> None:
        with self.assertRaises(SystemExit):
            self._run(PAYLOAD, file_feedback.BrokerAnswerLost("connection reset"))
        for broker in (urllib.error.URLError("refused"), (503, {"error": "busy"}), (502, {})):
            with self.subTest(broker=broker):
                code, printed, calls = self._run(PAYLOAD, broker)
                self.assertEqual((0, "UNCONFIRMED"), (code, printed["status"]))
                self.assertIn("recheck", printed["detail"])
                self.assertEqual(1, len(self._markers()), "the earlier request went out; the marker stays")

    def test_a_confirmed_marker_tells_the_recheck_the_form_confirmed(self) -> None:
        pending = {"status": "SUBMITTED", "issue_url": None, "detail": "not yet", "fallback": FALLBACK}
        self._run(PAYLOAD, (200, pending))
        _, _, calls = self._run(PAYLOAD, (200, pending))
        self.assertEqual([(True, True)], [(c.get("recheck_only"), c.get("form_confirmed")) for c in calls])

    def test_a_broken_body_read_of_a_refusal_is_a_lost_answer(self) -> None:
        import http.client
        import io

        class CutError(urllib.error.HTTPError):
            def read(self, *_a, **_k):
                raise http.client.IncompleteRead(b"{")

        cut = CutError("http://broker/v1/feedback/submit", 503, "busy", {}, io.BytesIO(b""))
        with mock.patch.object(file_feedback.credential_proxy_client, "open_broker_request", side_effect=cut):
            with self.assertRaises(file_feedback.BrokerAnswerLost):
                file_feedback._post_to_broker(file_feedback._build_request(BROKER, dict(PAYLOAD)))

    def test_a_marker_holding_non_object_json_is_an_attempted_marker(self) -> None:
        path = self.state_dir / "payload.json"
        path.write_text(json.dumps(PAYLOAD), encoding="utf-8")
        marker = file_feedback._marker_path(self.state_dir, file_feedback.feedback_broker.normalize(PAYLOAD))
        marker.write_text("null", encoding="utf-8")
        code, printed, calls = self._run(PAYLOAD, (200, ALREADY))
        self.assertEqual((0, "ALREADY_SUBMITTED"), (code, printed["status"]))
        self.assertEqual([True], [c.get("recheck_only") for c in calls])

    def test_a_broken_body_read_of_a_success_is_a_lost_answer(self) -> None:
        class Cut:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): raise ConnectionAbortedError("aborted")

        with mock.patch.object(file_feedback.credential_proxy_client, "open_broker_request", return_value=Cut()):
            with self.assertRaises(file_feedback.BrokerAnswerLost):
                file_feedback._post_to_broker(file_feedback._build_request(BROKER, dict(PAYLOAD)))

    def test_a_request_that_cannot_be_built_is_blocked_and_leaves_no_marker(self) -> None:
        # A scheme-less endpoint fails inside urllib's Request constructor, a
        # ValueError: nothing was sent, so no marker may stay behind.
        path = self.state_dir / "payload.json"
        path.write_text(json.dumps(PAYLOAD), encoding="utf-8")
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": "localhost"}):
            with contextlib.redirect_stdout(out):
                code = file_feedback.submit(path, self.state_dir)
        printed = json.loads(out.getvalue().strip())
        self.assertEqual((1, "BLOCKED"), (code, printed["status"]))
        self.assertEqual([], self._markers())

    def test_a_confirmed_recheck_carries_when_the_form_confirmed(self) -> None:
        pending = {"status": "SUBMITTED", "issue_url": None, "detail": "not yet", "fallback": FALLBACK}
        self._run(PAYLOAD, (200, pending))
        _, _, calls = self._run(PAYLOAD, (200, pending))
        self.assertIsInstance(calls[0].get("form_confirmed_at"), float)

    def test_the_payload_file_cannot_set_the_recheck_fields(self) -> None:
        code, printed, calls = self._run(dict(PAYLOAD, recheck_only=True, form_confirmed=True, form_confirmed_at=1.0),
                                         (200, SUBMITTED))
        self.assertEqual((0, "SUBMITTED"), (code, printed["status"]))
        self.assertEqual(1, len(calls))
        for control in ("recheck_only", "form_confirmed", "form_confirmed_at"):
            self.assertNotIn(control, calls[0])

    def test_the_token_is_read_once_and_before_the_marker(self) -> None:
        reads: list[int] = []

        def headers():
            reads.append(1)
            return {"Authorization": "Bearer t"}

        path = self.state_dir / "payload.json"
        path.write_text(json.dumps(PAYLOAD), encoding="utf-8")
        with mock.patch.object(file_feedback.credential_proxy_client, "authorization_headers", side_effect=headers):
            with mock.patch.object(file_feedback, "_send", return_value=(200, SUBMITTED)):
                with contextlib.redirect_stdout(io.StringIO()):
                    file_feedback.submit(path, self.state_dir)
        self.assertEqual(1, len(reads))

    def test_missing_endpoint_is_blocked(self) -> None:
        with mock.patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": ""}):
            code, printed, calls = self._run(PAYLOAD, (200, SUBMITTED))
        self.assertEqual((1, "BLOCKED"), (code, printed["status"]))
        self.assertIn("CREDENTIAL_PROXY_URL", printed["reason"])
        self.assertEqual(([], []), (calls, self._markers()))


if __name__ == "__main__":
    unittest.main()
