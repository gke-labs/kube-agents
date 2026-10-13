# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The relay-report plant's in-pod script, run against a stub route.

`bench/tf/prebuilt/relay-report` hands one report to the chat relay with
relay_probe.py. It runs here as it does in the pod, `python3 - <args> < relay_probe.py`,
against a local HTTP server standing in for the Session KV server's /v1/cron-reports
and a stub `hermes` that records the cards it archives. Pinned: the request the post
makes, the state it records for the verifier whatever the route answers, and a clean
that archives only the probe's cards. The provisioners' bash is syntax-checked as
Terraform renders it.
"""

import base64
import http.server
import json
import os
import pathlib
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
STACK = REPO / "bench" / "tf" / "prebuilt" / "relay-report"
PROBE = STACK / "relay_probe.py"
HEREDOC = re.compile(r"command\s+=\s+<<-EOT\n(.*?)\n\s*EOT", re.S)
KEY = "test-session-kv-key"


def render(script: str) -> str:
    """A provisioner's bash as Terraform hands it over, every interpolation a placeholder."""
    return re.sub(r"(?<!\$)\$\{[^}]*\}", "X", script).replace("$${", "${")


class Route(http.server.BaseHTTPRequestHandler):
    status = 200
    answer = {"status": "delivered", "relay": "ok", "relay_detail": ""}
    seen: list = []

    def do_POST(self):  # noqa: N802 - http.server's name
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Route.seen.append((self.path, self.headers.get("Authorization"), body))
        payload = json.dumps(Route.answer).encode()
        self.send_response(Route.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


class RelayProbeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = pathlib.Path(self._tmp.name)
        self.home = root / "data"
        self.home.mkdir()
        self.archived = root / "archived"
        self.hermes = root / "hermes"
        self.hermes.write_text(f'#!/bin/sh\necho "$@" >> "{self.archived}"\n')
        self.hermes.chmod(self.hermes.stat().st_mode | stat.S_IXUSR)
        Route.status, Route.seen = 200, []
        self.server = http.server.HTTPServer(("127.0.0.1", 0), Route)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.report = (STACK / "report.txt").read_text()
        self.kv_db = root / "session_kv.db"

    def probe(self, *args, key=KEY):
        env = {k: v for k, v in os.environ.items() if k != "SESSION_KV_API_KEY"}
        env["SESSION_KV_DB_PATH"] = str(self.kv_db)
        if key:
            env["SESSION_KV_API_KEY"] = key
        return subprocess.run(
            [sys.executable, "-", args[0], str(self.home), str(self.hermes), self.base, *args[1:]],
            input=PROBE.read_text(), capture_output=True, text=True, env=env, check=False,
        )

    def post(self):
        return self.probe("post", base64.b64encode(self.report.encode()).decode())

    def state(self):
        return json.loads((self.home / ".bench-relay-probe.json").read_text())

    def test_the_post_is_the_relay_plugins_call(self):
        done = self.post()

        self.assertEqual(done.returncode, 0, done.stderr)
        ((path, auth, body),) = Route.seen
        self.assertEqual(path, "/v1/cron-reports")
        self.assertEqual(auth, f"Bearer {KEY}")
        self.assertEqual(body["job_id"], "bench-relay-probe")
        self.assertEqual(body["profile"], "platform")
        self.assertEqual(body["report"], self.report)
        state = self.state()
        self.assertEqual(state["status"], 200)
        self.assertEqual(state["answer"]["status"], "delivered")
        self.assertEqual(state["report"], self.report)

    def test_a_502_is_recorded_for_the_verifier_not_raised(self):
        Route.status, Route.answer = 502, {"detail": "chat relay failed: composed but not delivered to google_chat"}
        self.addCleanup(setattr, Route, "answer", {"status": "delivered", "relay": "ok", "relay_detail": ""})

        done = self.post()

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.state()["status"], 502)
        self.assertIn("composed but not delivered", self.state()["answer"]["detail"])

    def test_no_key_refuses_before_posting(self):
        done = self.probe("post", base64.b64encode(b"x").decode(), key="")

        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(Route.seen, [])

    def test_clean_archives_only_the_probes_cards_and_removes_the_state(self):
        self.post()
        with sqlite3.connect(self.home / "kanban.db") as con:
            con.execute("CREATE TABLE tasks (id TEXT, session_id TEXT, status TEXT)")
            con.execute("INSERT INTO tasks VALUES ('t_mine', 'cron-platform-bench-relay-probe-20261013', 'ready')")
            con.execute("INSERT INTO tasks VALUES ('t_other', 'cron-platform-stall-watch-20261013', 'ready')")
            con.execute("INSERT INTO tasks VALUES ('t_gone', 'cron-platform-bench-relay-probe-20261012', 'archived')")

        done = self.probe("clean")

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse((self.home / ".bench-relay-probe.json").exists())
        self.assertEqual(self.archived.read_text().split(), ["kanban", "archive", "t_mine"])

    def test_clean_drops_the_probe_from_the_report_index_and_nothing_else(self):
        """A probe row left behind is listed beside every unthreaded message for a day."""
        mine = {"platform_threads": {"google_chat": {"chat_id": "spaces/A", "thread_id": "spaces/A/threads/P"}}}
        other = {"platform_threads": {"google_chat": {"chat_id": "spaces/A", "thread_id": "spaces/A/threads/O"}}}
        with sqlite3.connect(self.kv_db) as con:
            con.execute("CREATE TABLE session_metadata (session_id TEXT PRIMARY KEY, metadata TEXT)")
            con.execute("CREATE TABLE incidents (chat_id TEXT, thread_id TEXT, report TEXT)")
            con.execute("INSERT INTO session_metadata VALUES (?, ?)", ("cron-platform-bench-relay-probe-20261013", json.dumps(mine)))
            con.execute("INSERT INTO session_metadata VALUES (?, ?)", ("cron-platform-stall-watch-20261013", json.dumps(other)))
            con.execute("INSERT INTO incidents VALUES ('spaces/A', 'spaces/A/threads/P', 'probe report')")
            con.execute("INSERT INTO incidents VALUES ('spaces/A', 'spaces/A/threads/O', 'real report')")

        done = self.probe("clean")

        self.assertEqual(done.returncode, 0, done.stderr)
        with sqlite3.connect(self.kv_db) as con:
            self.assertEqual(
                [r[0] for r in con.execute("SELECT session_id FROM session_metadata")],
                ["cron-platform-stall-watch-20261013"],
            )
            self.assertEqual([r[0] for r in con.execute("SELECT report FROM incidents")], ["real report"])

    def test_a_clean_with_nothing_posted_succeeds(self):
        done = self.probe("clean")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_provisioners_parse_as_bash(self):
        scripts = HEREDOC.findall((STACK / "main.tf").read_text())
        # The apply and the teardown; a pattern that stops matching would otherwise check nothing.
        self.assertEqual(len(scripts), 2)
        for script in scripts:
            done = subprocess.run(["bash", "-n"], input=render(script), capture_output=True, text=True, check=False)
            self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main()
