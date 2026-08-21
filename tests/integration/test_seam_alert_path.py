"""Seam: event inject → session KV → chat alert → routing → agent turn.

The real `session_kv_server` runs as it does in the pod; the two components it
talks OUT to are faked exactly where the seam ends — `hermes` is an
argv-recording executable on PATH, and the gateway is a stub HTTP server. This
is the alert path the maintainers report as broken end-to-end in production,
and the path where every hop fails open: `_post_initial_alert` swallows every
exception and returns None, the quota check fails open, and routing, session
creation, and the agent turn each log-and-continue. A dead path is therefore
invisible by construction — which is what the expectedFailure test at the
bottom pins.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from _seams import (
    API_KEY,
    KVServer,
    RecordingHTTPServer,
    fake_executable,
    http_json,
    wait_until,
)

INJECT_PAYLOAD = {
    "kind": "event",
    "reason": "CrashLoopBackOff",
    "namespace": "payments",
    "kind_of_object": "Pod",
    "name": "payments-api-6cfdb6b98b-zwv24",
    "message": "back-off restarting failed container",
    "count": 7,
    "type": "Warning",
}

# What the real `hermes send --json` prints for a Google Chat post; the thread
# derivation in `_post_initial_alert` splits it on /messages/.
HERMES_MESSAGE_ID = "spaces/AAA-space/messages/thr-123.msg-456"


def _record_and_reply(record_path: Path, exit_code: int = 0) -> str:
    return f"""
        import json, sys
        from pathlib import Path
        record = Path({str(record_path)!r})
        with record.open("a") as handle:
            handle.write(json.dumps(sys.argv[1:]) + "\\n")
        if {exit_code} != 0:
            sys.stderr.write("hermes: transport failed\\n")
            sys.exit({exit_code})
        print(json.dumps({{"message_id": {HERMES_MESSAGE_ID!r}}}))
    """


class AlertPathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        self.hermes_calls = self.tmp_path / "hermes-argv.jsonl"
        self.gateway = RecordingHTTPServer(
            responses={"/api/sessions": (200, {"status": "ok"})}
        )
        self.addCleanup(self.gateway.stop)

    def start_kv(self, hermes_exit=0, env=None):
        bin_dir = self.tmp_path / "bin"
        fake_executable(bin_dir, "hermes", _record_and_reply(self.hermes_calls, hermes_exit))
        server_env = {"PLATFORM_API_URL": self.gateway.url}
        if env:
            server_env.update(env)
        kv = KVServer(self.tmp_path, env=server_env, path_prepend=str(bin_dir))
        self.addCleanup(kv.stop)
        return kv

    def _create_session(self, kv):
        status, body = http_json(f"{kv.url}/sessions", payload={}, method="POST")
        self.assertEqual(201, status)
        return body["sessionID"]

    def _inject(self, kv, session_id, payload=INJECT_PAYLOAD):
        return http_json(
            f"{kv.url}/sessions/{session_id}/inject",
            payload={"message": json.dumps(payload)},
        )

    def _hermes_argvs(self):
        if not self.hermes_calls.exists():
            return []
        return [
            json.loads(line)
            for line in self.hermes_calls.read_text().splitlines()
            if line.strip()
        ]

    def test_an_injected_event_reaches_chat_routing_and_the_agent_turn(self):
        kv = self.start_kv()
        session_id = self._create_session(kv)
        status, body = self._inject(kv, session_id)
        self.assertEqual(200, status)
        self.assertEqual("injected", body["status"])

        # The chat post: hermes send --json --to google_chat <alert>, with the
        # parsed payload fields visible in the rendered alert. This is the
        # double-JSON envelope arriving intact.
        wait_until(lambda: self._hermes_argvs(), message="the hermes chat post")
        argv = self._hermes_argvs()[0]
        self.assertEqual(["send", "--json", "--to", "google_chat"], argv[:4])
        alert_text = argv[4]
        self.assertIn("payments/payments-api", alert_text)
        self.assertIn("Crash loop back off", alert_text)
        self.assertIn("Critical", alert_text)

        # The routing row: thread derived from the hermes message id, chat_id
        # from its space part — the address the triage card's report returns to.
        def routed():
            status, meta = http_json(
                f"{kv.url}/v1/sessions/{session_id}/metadata"
            )
            return status == 200 and meta.get("thread_id")

        wait_until(routed, message="the session routing row")
        _, meta = http_json(f"{kv.url}/v1/sessions/{session_id}/metadata")
        self.assertEqual("spaces/AAA-space/threads/thr-123", meta["thread_id"])
        self.assertEqual("spaces/AAA-space", meta["chat_id"])
        self.assertEqual("google_chat", meta["platform"])

        # The agent turn: session created on the gateway, then the chat turn,
        # in that order, with the session id in both paths.
        wait_until(
            lambda: any("/chat" in p for p in self.gateway.paths("POST")),
            message="the gateway agent turn",
        )
        posts = self.gateway.paths("POST")
        self.assertIn("/api/sessions", posts)
        self.assertIn(f"/api/sessions/{session_id}/chat", posts)
        self.assertLess(
            posts.index("/api/sessions"),
            posts.index(f"/api/sessions/{session_id}/chat"),
        )

    def test_the_daily_ceiling_suppresses_and_reports_rather_than_posting(self):
        kv = self.start_kv(env={"ALERT_DAILY_LIMIT_CRITICAL": "2"})
        for expected in ("injected", "injected", "suppressed"):
            session_id = self._create_session(kv)
            status, body = self._inject(kv, session_id)
            self.assertEqual(200, status)
            self.assertEqual(expected, body["status"])

        # 200-with-suppressed, never an error code: the watcher must drop its
        # dedup entry rather than counting this against its inject-error metric.
        status, quota = http_json(f"{kv.url}/v1/alert-quota")
        self.assertEqual(200, status)
        # The endpoint's shape is a contract in this repo, not something to
        # probe for: get_alert_quota returns {"day", "severities"} keyed by
        # severity. An earlier revision guarded on a "quota" key the endpoint
        # never emits, so the real assertions below sat in an unreachable
        # branch and the accounting would have stayed green recording nothing.
        critical = quota["severities"]["Critical"]
        self.assertEqual(2, critical["limit"])
        self.assertEqual(2, critical["sent"])
        self.assertEqual(1, critical["suppressed"])

    def test_a_hermes_already_on_the_runners_path_is_never_the_one_that_runs(self):
        """The workstation case: `make test-integration` must not post to chat.

        A Critical inject shells out to a bare `hermes send`, resolved through
        the server's PATH, and a maintainer running the AGENTS.md live
        validation has a configured `hermes` on theirs. This stands one in the
        runner's own PATH — not via `path_prepend` — and asserts the guard
        `KVServer` installs shadows it. A test that wants the call recorded
        passes its fake through `path_prepend`, which is searched first; that
        is what every other test in this file relies on.
        """
        guarded = self.tmp_path / "guarded"
        guarded.mkdir()
        workstation_bin = self.tmp_path / "workstation-bin"
        posted = self.tmp_path / "posted-for-real.jsonl"
        fake_executable(workstation_bin, "hermes", _record_and_reply(posted))

        kv = KVServer(
            guarded,
            env={
                "PLATFORM_API_URL": self.gateway.url,
                "PATH": f"{os.environ.get('PATH', '')}{os.pathsep}{workstation_bin}",
                # The other half of the hazard: with a token in the
                # environment, `get_active_platform` returns "slack" and the
                # send targets whatever the machine is really configured for.
                "SLACK_BOT_TOKEN": "xoxb-the-runners-real-token",
            },
        )
        self.addCleanup(kv.stop)
        session_id = self._create_session(kv)
        status, _ = self._inject(kv, session_id)
        self.assertEqual(200, status)

        # Wait for the pipeline to run all the way through, so "nothing was
        # posted" is a finished pipeline rather than an unfinished one.
        wait_until(
            lambda: any("/chat" in p for p in self.gateway.paths("POST")),
            message="the pipeline to run to completion",
        )
        self.assertFalse(
            posted.exists(),
            "the runner's own hermes was invoked -- a real chat post from a test run",
        )

    @unittest.expectedFailure
    def test_a_failed_chat_post_leaves_a_visible_record_not_silence(self):
        """DESIRED, not current, behaviour — the maintainer-reported breakage.

        When the chat post fails, today's path is: `_post_initial_alert` logs
        and returns None; routing is skipped; the agent turn still fires; the
        inject still answers "injected"; and nothing queryable records that a
        human was never told. Five fail-opens in a row make a broken alert
        path indistinguishable from a healthy one — which is precisely how the
        production alert flow stayed broken "for a while" before anyone
        noticed (the class of silent breakage issue #863 is the deploy-side
        cousin of).

        The contract this test pins: a delivery failure must leave a mark that
        a liveness check can read — here, a `delivery_failed` field on the
        session's routing metadata. It flips from expectedFailure to green the
        day the path stops swallowing the failure.
        """
        kv = self.start_kv(hermes_exit=1)
        session_id = self._create_session(kv)
        status, body = self._inject(kv, session_id)
        self.assertEqual(200, status)

        # The turn still fires (deliberate: triage without a thread beats no
        # triage), so wait for the pipeline to finish before judging.
        wait_until(
            lambda: any("/chat" in p for p in self.gateway.paths("POST")),
            message="the pipeline to run to completion",
        )
        _, meta = http_json(f"{kv.url}/v1/sessions/{session_id}/metadata")
        self.assertTrue(
            meta.get("delivery_failed"),
            "a failed chat post must be recorded on the session, not swallowed",
        )


if __name__ == "__main__":
    unittest.main()
