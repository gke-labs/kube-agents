"""Unit tests for slack_blocks_post — Block Kit through the credential proxy's Slack relay.

Run: python3 -m pytest agents/platform/scripts/test_slack_blocks_post.py
"""

import io
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import slack_blocks_post as sbp

RELAY = "http://127.0.0.1:8765"
BLOCKS = [{"type": "divider"}]


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(body: dict, code: int = 502) -> urllib.error.HTTPError:
    raw = json.dumps(body).encode("utf-8")
    return urllib.error.HTTPError(RELAY, code, "Bad Gateway", {}, io.BytesIO(raw))


class PostTest(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {sbp.RELAY_ENV: RELAY})
        env.start()
        self.addCleanup(env.stop)
        # A pod's token file would add an auth header the request assertions do not expect.
        os.environ.pop("CREDENTIAL_PROXY_TOKEN_FILE", None)

    def _post(self, answer, thread_ts=""):
        effect = answer if isinstance(answer, Exception) else None
        response = None if effect else _Response(json.dumps({"response": answer}).encode("utf-8"))
        with patch.object(sbp.urllib.request, "urlopen", return_value=response, side_effect=effect) as urlopen:
            ts = sbp.post("C1", "fallback", BLOCKS, thread_ts)
        return ts, urlopen.call_args.args[0]

    def test_posts_blocks_through_the_relay_and_returns_the_ts(self):
        ts, request = self._post({"ok": True, "ts": "1.2"}, thread_ts="0.9")
        self.assertEqual(ts, "1.2")
        self.assertEqual(request.full_url, RELAY + sbp.RELAY_API_PATH)
        payload = json.loads(request.data)
        self.assertEqual(payload["method"], "chat.postMessage")
        self.assertEqual(
            payload["arguments"]["json"],
            {"channel": "C1", "text": "fallback", "blocks": BLOCKS, "thread_ts": "0.9"},
        )

    def test_an_ok_without_a_ts_still_counts_as_posted(self):
        ts, _ = self._post({"ok": True})
        self.assertEqual(ts, "")

    def test_slack_saying_no_is_refused(self):
        with self.assertRaisesRegex(sbp.Refused, "invalid_blocks"):
            self._post({"ok": False, "error": "invalid_blocks"})

    def test_only_block_errors_justify_fewer_blocks(self):
        self.assertTrue(sbp.blocks_refused(sbp.Refused("invalid_blocks")))
        for code in ("ratelimited", "channel_not_found", "msg_too_long"):
            self.assertFalse(sbp.blocks_refused(sbp.Refused(code)))

    def test_a_relayed_slack_error_is_refused(self):
        with self.assertRaisesRegex(sbp.Refused, "invalid_blocks"):
            self._post(_http_error({"error": "slack_api_error", "slack": {"error": "invalid_blocks"}}))

    def test_a_relay_server_error_may_have_posted(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._post(_http_error({"error": "upstream timeout"}))
        self.assertNotIsInstance(caught.exception, (sbp.Refused, sbp.NotSent))

    def test_a_relay_client_error_was_not_sent(self):
        with self.assertRaisesRegex(sbp.NotSent, "401"):
            self._post(_http_error({"error": "unauthorized"}, code=401))

    def test_a_failure_before_sending_was_not_sent(self):
        with self.assertRaisesRegex(sbp.NotSent, "connection refused"):
            self._post(urllib.error.URLError("connection refused"))

    def test_an_unreadable_broker_token_was_not_sent(self):
        unreadable = sbp.TokenUnavailable("token file is empty")
        with patch.object(sbp, "authorization_headers", side_effect=unreadable), patch.object(
            sbp.urllib.request, "urlopen"
        ) as urlopen, self.assertRaisesRegex(sbp.NotSent, "token file is empty"):
            sbp.post("C1", "fallback", BLOCKS)
        urlopen.assert_not_called()

    def test_a_timeout_reading_the_answer_may_have_posted(self):
        with self.assertRaises(TimeoutError):
            self._post(TimeoutError("timed out"))

    def test_no_relay_posts_nothing(self):
        os.environ[sbp.RELAY_ENV] = " "
        self.assertFalse(sbp.configured())
        with patch.object(sbp.urllib.request, "urlopen") as urlopen, self.assertRaises(sbp.NotConfigured):
            sbp.post("C1", "fallback", BLOCKS)
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
