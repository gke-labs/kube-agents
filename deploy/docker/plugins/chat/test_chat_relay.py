"""Unit tests for the ``deliver: "chat"`` platform plugin.

Covers ``adapter.py`` on its own. What it cannot cover is that Hermes resolves
``deliver: "chat"`` to this plugin at all — that is
``deploy/docker/plugins/verify_chat_relay.py``, which drives the real
``cron/scheduler.py::_deliver_result`` against the installed tree at image build
time.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import adapter as mod


class RecordingRelay:
    """A stdlib HTTP server standing in for the Session KV server."""

    def __init__(self, status: int = 200, body: bytes = b"{}") -> None:
        self.status = status
        self.body = body
        self.requests: list[dict] = []
        server_self = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — stdlib naming
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length).decode("utf-8")
                server_self.requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization", ""),
                        "body": json.loads(raw) if raw else {},
                    }
                )
                self.send_response(server_self.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(server_self.body)))
                self.end_headers()
                self.wfile.write(server_self.body)

            def log_message(self, *_args) -> None:
                """Keep the test output clean."""

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "RecordingRelay":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[0], self._server.server_address[1]
        return f"http://{host}:{port}/v1/cron-reports"


def wrapped(title: str, job_id: str, body: str) -> str:
    """``_deliver_result``'s wrapper, byte for byte."""
    return (
        f"Cronjob Response: {title}\n"
        f"(job_id: {job_id})\n"
        f"-------------\n\n"
        f"{body}\n\n"
        f'To stop or manage this job, send me a new message (e.g. "stop reminder {title}").'
    )


class TestParseCronWrapper(unittest.TestCase):
    def test_the_wrapper_yields_id_title_and_a_clean_report(self):
        job_id, title, report = mod.parse_cron_wrapper(
            wrapped("GitHub Repo Watcher", "github-repo-watcher", "the issues sweep failed")
        )
        self.assertEqual(job_id, "github-repo-watcher")
        self.assertEqual(title, "GitHub Repo Watcher")
        self.assertEqual(report, "the issues sweep failed")

    def test_a_multi_line_report_keeps_its_shape(self):
        body = "## Findings\n\n- one\n- two\n\n```\ncode\n```"
        _, _, report = mod.parse_cron_wrapper(wrapped("Audit", "a", body))
        self.assertEqual(report, body)

    def test_a_report_that_itself_mentions_the_footer_text(self):
        body = 'Tell the user: To stop or manage this job, send me a new message (e.g. "x").'
        _, _, report = mod.parse_cron_wrapper(wrapped("Audit", "a", body))
        self.assertEqual(report, body, "only the trailing footer may be stripped")

    def test_no_wrapper_relays_the_whole_message_anonymously(self):
        job_id, title, report = mod.parse_cron_wrapper("just the report")
        self.assertEqual((job_id, title), ("", ""))
        self.assertEqual(report, "just the report")

    def test_an_empty_message(self):
        self.assertEqual(mod.parse_cron_wrapper(""), ("", "", ""))

    def test_a_header_like_first_line_that_is_not_the_wrapper(self):
        self.assertEqual(
            mod.parse_cron_wrapper("Cronjob Response: x\nbut no job_id line"),
            ("", "", "Cronjob Response: x\nbut no job_id line"),
        )


class TestProfileName(unittest.TestCase):
    def test_a_named_profile(self):
        with patch.dict(os.environ, {"HERMES_HOME": "/opt/data/profiles/platform"}):
            self.assertEqual(mod.profile_name(), "platform")

    def test_a_cluster_profile(self):
        with patch.dict(os.environ, {"HERMES_HOME": "/opt/data/profiles/cluster-prod-a"}):
            self.assertEqual(mod.profile_name(), "cluster-prod-a")

    def test_the_root_home_is_not_called_data(self):
        with patch.dict(os.environ, {"HERMES_HOME": "/opt/data"}):
            self.assertEqual(mod.profile_name(), "default")

    def test_an_unset_home(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(mod.profile_name(), "default")


class TestIsConnected(unittest.TestCase):
    """The one switch. Unset in the gateway, set by ``profile_cron_tick.py``."""

    def test_unset_keeps_the_platform_out_of_the_gateway(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(mod.is_connected(None))

    def test_blank_is_unset(self):
        with patch.dict(os.environ, {mod.HOME_CHANNEL_ENV: "   "}):
            self.assertFalse(mod.is_connected(None))

    def test_set_switches_the_relay_on(self):
        with patch.dict(os.environ, {mod.HOME_CHANNEL_ENV: "cron-reports"}):
            self.assertTrue(mod.is_connected(None))

    def test_there_is_no_adapter_to_build(self):
        with self.assertRaises(NotImplementedError):
            mod._no_adapter(None)


class TestStandaloneSend(unittest.TestCase):
    MESSAGE = wrapped("GitHub Repo Watcher", "github-repo-watcher", "the issues sweep failed")

    def test_a_report_reaches_the_route_with_its_key(self):
        with RecordingRelay() as relay:
            with patch.dict(
                os.environ,
                {
                    "SESSION_KV_API_KEY": "k",
                    "CRON_REPORT_RELAY_URL": relay.url,
                    "HERMES_HOME": "/opt/data/profiles/platform",
                },
            ):
                result = asyncio.run(
                    mod.standalone_send(None, "cron-reports", self.MESSAGE)
                )
            self.assertTrue(result.get("success"), result)
            self.assertEqual(len(relay.requests), 1)
            sent = relay.requests[0]
            self.assertEqual(sent["path"], "/v1/cron-reports")
            self.assertEqual(sent["authorization"], "Bearer k")
            self.assertEqual(
                sent["body"],
                {
                    "job_id": "github-repo-watcher",
                    "profile": "platform",
                    "title": "GitHub Repo Watcher",
                    "report": "the issues sweep failed",
                },
            )

    def test_the_cron_wrapper_never_reaches_the_chat_agent(self):
        """It would ask the Chat Agent to relay Hermes' own plumbing text."""
        with RecordingRelay() as relay:
            with patch.dict(
                os.environ,
                {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
            ):
                asyncio.run(mod.standalone_send(None, "c", self.MESSAGE))
            report = relay.requests[0]["body"]["report"]
        self.assertNotIn("Cronjob Response:", report)
        self.assertNotIn("To stop or manage this job", report)

    def test_an_unwrapped_message_still_relays(self):
        with RecordingRelay() as relay:
            with patch.dict(
                os.environ,
                {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
            ):
                result = asyncio.run(mod.standalone_send(None, "c", "bare report"))
            self.assertTrue(result.get("success"), result)
            self.assertEqual(relay.requests[0]["body"]["report"], "bare report")
            self.assertEqual(relay.requests[0]["body"]["job_id"], "")

    def test_an_empty_report_is_a_silent_tick(self):
        """`github-repo-watcher` prints nothing on a clean sweep, 144 times a day."""
        with RecordingRelay() as relay:
            with patch.dict(
                os.environ,
                {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
            ):
                result = asyncio.run(
                    mod.standalone_send(
                        None, "c", wrapped("GitHub Repo Watcher", "ghw", "")
                    )
                )
            self.assertTrue(result.get("success"), result)
            self.assertEqual(result.get("skipped"), "empty_report")
            self.assertEqual(relay.requests, [], "nothing should have been sent")

    def test_a_whitespace_report_is_silence_too(self):
        with RecordingRelay() as relay:
            with patch.dict(
                os.environ,
                {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
            ):
                result = asyncio.run(mod.standalone_send(None, "c", "   \n\t "))
            self.assertEqual(result.get("skipped"), "empty_report")
            self.assertEqual(relay.requests, [])

    def test_silence_is_not_a_missing_key(self):
        """A quiet tick has nothing to authenticate, so an unset key is not its problem.

        Otherwise the guard would just trade one every-ten-minutes
        ``last_delivery_error`` for another.
        """
        with patch.dict(os.environ, {}, clear=True):
            result = asyncio.run(mod.standalone_send(None, "c", ""))
        self.assertTrue(result.get("success"), result)
        self.assertNotIn("error", result)

    def test_no_key_is_refused_before_the_request(self):
        with RecordingRelay() as relay:
            with patch.dict(
                os.environ, {"CRON_REPORT_RELAY_URL": relay.url}, clear=True
            ):
                result = asyncio.run(mod.standalone_send(None, "c", "r"))
            self.assertIn("SESSION_KV_API_KEY", result.get("error", ""))
            self.assertEqual(relay.requests, [], "nothing should have been sent")

    def test_a_server_error_is_reported_not_raised(self):
        with RecordingRelay(status=500) as relay:
            with patch.dict(
                os.environ,
                {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
            ):
                result = asyncio.run(mod.standalone_send(None, "c", "r"))
        self.assertIn("500", result.get("error", ""))

    def test_an_unreachable_relay_is_reported_not_raised(self):
        with patch.dict(
            os.environ,
            {
                "SESSION_KV_API_KEY": "k",
                # Port 1 is reserved and never listening.
                "CRON_REPORT_RELAY_URL": "http://127.0.0.1:1/v1/cron-reports",
            },
        ):
            result = asyncio.run(mod.standalone_send(None, "c", "r"))
        self.assertIn("unreachable", result.get("error", "").lower())

    def test_no_failure_string_carries_the_key(self):
        """These strings end up in ``last_delivery_error`` and in the log."""
        secret = "s3cr3t-session-kv-key"
        with RecordingRelay(status=503) as relay:
            with patch.dict(
                os.environ,
                {"SESSION_KV_API_KEY": secret, "CRON_REPORT_RELAY_URL": relay.url},
            ):
                result = asyncio.run(mod.standalone_send(None, "c", "r"))
        self.assertNotIn(secret, json.dumps(result))

    def test_every_failure_is_a_dict_send_message_understands(self):
        """``_send_via_adapter`` requires ``success`` or ``error`` — never a raise."""
        with patch.dict(
            os.environ,
            {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": "not-a-url"},
        ):
            result = asyncio.run(mod.standalone_send(None, "c", "r"))
        self.assertIsInstance(result, dict)
        self.assertTrue(result.get("success") or result.get("error"))

    def test_a_failure_names_the_leg_that_broke(self):
        """The route relays synchronously, so its verdict is the delivery result.

        A bare "HTTP 502" in `last_delivery_error` says a watchdog went quiet and
        nothing about why; the route's `detail` names the leg.
        """
        detail = b'{"detail":"chat relay failed: composed but not delivered to google_chat"}'
        with RecordingRelay(status=502, body=detail) as relay:
            with patch.dict(
                os.environ,
                {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
            ):
                result = asyncio.run(mod.standalone_send(None, "c", "r"))
        self.assertIn("502", result["error"])
        self.assertIn("composed but not delivered to google_chat", result["error"])

    def test_an_unparseable_error_body_still_reports_the_status(self):
        for body in (b"", b"<html>gateway timeout</html>", b'{"detail":null}', b'{"detail":"  "}'):
            with self.subTest(body=body):
                with RecordingRelay(status=502, body=body) as relay:
                    with patch.dict(
                        os.environ,
                        {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
                    ):
                        result = asyncio.run(mod.standalone_send(None, "c", "r"))
                self.assertEqual(result["error"], "chat relay answered HTTP 502")

    def test_a_long_detail_is_bounded(self):
        """It is stored per job run, so it cannot be a whole report."""
        detail = json.dumps({"detail": "x" * 5000}).encode()
        with RecordingRelay(status=502, body=detail) as relay:
            with patch.dict(
                os.environ,
                {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
            ):
                result = asyncio.run(mod.standalone_send(None, "c", "r"))
        self.assertLess(len(result["error"]), 300)

    def test_a_composed_delivery_is_a_plain_success(self):
        """The route says `relay: "ok"` on the healthy path."""
        body = b'{"status":"delivered","relay":"ok","session_id":"s1"}'
        with RecordingRelay(body=body) as relay:
            with patch.dict(
                os.environ,
                {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
            ):
                result = asyncio.run(mod.standalone_send(None, "c", self.MESSAGE))
        self.assertTrue(result.get("success"), result)
        self.assertNotIn("error", result)

    def test_a_degraded_relay_is_recorded_as_a_delivery_error(self):
        """200 plus `relay: "degraded"` means posted raw, not composed.

        `error` is the only field the scheduler reads, and `last_delivery_error`
        the only place a run record can carry it — so a front door that has been
        down all week must not produce run records identical to healthy ones.
        """
        body = b'{"status":"delivered","relay":"degraded","session_id":"s1"}'
        with RecordingRelay(body=body) as relay:
            with patch.dict(
                os.environ,
                {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
            ):
                result = asyncio.run(mod.standalone_send(None, "c", self.MESSAGE))
        self.assertNotIn("success", result)
        self.assertIn("degraded", result["error"])

    def test_the_degraded_string_says_the_report_did_arrive(self):
        """Otherwise `cronjob list` reads as "nothing was sent" and invites a
        re-run, which would post the same finding to the channel twice."""
        body = b'{"status":"delivered","relay":"degraded"}'
        with RecordingRelay(body=body) as relay:
            with patch.dict(
                os.environ,
                {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
            ):
                result = asyncio.run(mod.standalone_send(None, "c", "r"))
        error = result["error"]
        self.assertIn("was posted", error)
        self.assertIn("do not re-run", error.lower())

    def test_a_2xx_that_says_nothing_about_the_relay_is_a_success(self):
        """An older route, or one that answered before the field existed."""
        for body in (b"{}", b"", b"<html>ok</html>", b'{"relay":null}', b"[]"):
            with self.subTest(body=body):
                with RecordingRelay(body=body) as relay:
                    with patch.dict(
                        os.environ,
                        {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
                    ):
                        result = asyncio.run(mod.standalone_send(None, "c", "r"))
                self.assertTrue(result.get("success"), result)

    def test_a_verdict_never_turns_a_failure_into_a_success(self):
        """A non-2xx body is not read for a verdict — the status decides."""
        body = b'{"relay":"ok"}'
        with RecordingRelay(status=502, body=body) as relay:
            with patch.dict(
                os.environ,
                {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
            ):
                result = asyncio.run(mod.standalone_send(None, "c", "r"))
        self.assertIn("502", result["error"])

    def test_post_returns_an_error_and_a_verdict(self):
        """Every path out of `_post` is a 2-tuple; the callers unpack it."""
        with RecordingRelay(body=b'{"relay":"degraded"}') as relay:
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(mod._post(relay.url, {}, "k"), (None, "degraded"))
        with patch.dict(os.environ, {}, clear=True):
            error, verdict = mod._post("not-a-url", {}, "k")
        self.assertIsNotNone(error)
        self.assertEqual(verdict, "")

    def test_the_timeout_outlasts_a_chat_agent_turn(self):
        """Time out before the route answers and a delivered report is recorded
        as a failure. `_run_relay_turn` allows the turn itself 300s."""
        self.assertGreater(mod.RELAY_TIMEOUT_SECONDS, 300.0)

    def test_the_default_route_is_the_loopback_session_kv_server(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(mod.relay_url(), mod.DEFAULT_RELAY_URL)
        self.assertTrue(mod.DEFAULT_RELAY_URL.startswith("http://127.0.0.1:8699/"))


class TestRegistration(unittest.TestCase):
    """What the scheduler reads off the ``PlatformEntry``."""

    def test_the_entry_carries_what_cron_delivery_needs(self):
        captured = {}

        class Ctx:
            def register_platform(self, **kwargs):
                captured.update(kwargs)

        mod.register(Ctx())
        self.assertEqual(captured["name"], "chat")
        self.assertEqual(captured["cron_deliver_env_var"], mod.HOME_CHANNEL_ENV)
        self.assertIs(captured["standalone_sender_fn"], mod.standalone_send)
        self.assertIs(captured["is_connected"], mod.is_connected)

    def test_the_platform_name_matches_this_directory(self):
        """``Platform._missing_`` admits a plugin platform by directory name."""
        self.assertEqual(
            mod.PLATFORM_NAME, os.path.basename(os.path.dirname(os.path.abspath(mod.__file__)))
        )

    def test_reports_are_never_chunked(self):
        """A split report would start one Chat Agent turn per piece."""
        captured = {}

        class Ctx:
            def register_platform(self, **kwargs):
                captured.update(kwargs)

        mod.register(Ctx())
        self.assertEqual(captured["max_message_length"], 0)


if __name__ == "__main__":
    unittest.main()
