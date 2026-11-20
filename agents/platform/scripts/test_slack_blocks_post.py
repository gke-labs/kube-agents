"""Unit tests for slack_blocks_post — Block Kit through the credential proxy's Slack relay.

Run: python3 -m pytest agents/platform/scripts/test_slack_blocks_post.py
"""

import http.server
import io
import json
import os
import socket
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock
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


class _LiveBroker:
    """A local HTTP server that records the method, the path and the `Authorization`
    header of each request and answers `{}`; and a proxy environment whose proxy is
    a closed port, for a test that the call does not take the proxy."""

    def __init__(self, case):
        seen = self.seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self):
                seen.append((self.command, self.path, self.headers.get("Authorization")))
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            do_GET = do_POST = _answer  # noqa: N815

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
        case.addCleanup(server.server_close)
        case.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_port}"
        with socket.socket() as closed:
            closed.bind(("127.0.0.1", 0))
            proxy = f"http://127.0.0.1:{closed.getsockname()[1]}"
        # A plain `urlopen` builds its opener at the first call and keeps it. The
        # test drops the kept one, so that a call that went back to `urlopen`
        # reads this environment and goes to the closed port.
        for patcher in (
            mock.patch.dict(os.environ, {"http_proxy": proxy, "HTTP_PROXY": proxy}),
            mock.patch.object(urllib.request, "_opener", None),
        ):
            patcher.start()
            case.addCleanup(patcher.stop)
        for name in ("no_proxy", "NO_PROXY"):
            os.environ.pop(name, None)


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
        with patch.object(sbp.urllib.request.OpenerDirector, "open", return_value=response, side_effect=effect) as urlopen:
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
            sbp.urllib.request.OpenerDirector, "open"
        ) as urlopen, self.assertRaisesRegex(sbp.NotSent, "token file is empty"):
            sbp.post("C1", "fallback", BLOCKS)
        urlopen.assert_not_called()

    def test_a_timeout_reading_the_answer_may_have_posted(self):
        with self.assertRaises(TimeoutError):
            self._post(TimeoutError("timed out"))

    def test_a_proxy_in_the_environment_is_not_used_to_reach_the_relay(self):
        # The relay is the credential broker, an in-cluster Service. With a proxy
        # variable and no NO_PROXY entry for its host, urllib hands the post and its
        # bearer token to the proxy.
        broker = _LiveBroker(self)
        os.environ[sbp.RELAY_ENV] = broker.url
        # The live server answers `{}`, which is no post confirmation: `Refused`
        # shows that its answer, and not a proxy's, came back.
        with patch.object(sbp, "authorization_headers", return_value={"Authorization": "Bearer t"}), self.assertRaises(sbp.Refused):
            sbp.post("C1", "fallback", BLOCKS)
        self.assertEqual([("POST", sbp.RELAY_API_PATH, "Bearer t")], broker.seen)

    def test_no_relay_posts_nothing(self):
        os.environ[sbp.RELAY_ENV] = " "
        self.assertFalse(sbp.configured())
        with patch.object(sbp.urllib.request.OpenerDirector, "open") as urlopen, self.assertRaises(sbp.NotConfigured):
            sbp.post("C1", "fallback", BLOCKS)
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
