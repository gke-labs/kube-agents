"""Seam: the `KVServer` fixture the seam files that need a KV server start it through.

The sweep runs every test file at once, so the fixture's contract is what keeps
the seam tier deterministic: the port the harness picks is the port the server
serves on, with nothing able to take it in between; a server that dies leaves
nothing listening behind it; and a start that fails says why, with the
server's log, before the temporary directory deletes it.
"""

from __future__ import annotations

import errno
import re
import socket
import tempfile
import unittest
from pathlib import Path

from _seams import LOOPBACK_HOST, KVServer, http_json

PORT_IN_MESSAGE = re.compile(r"session_kv_server on :(\d+)")
LOG_HEADING = "chars of its log:"
# A refused connection comes back at once; this only bounds a hang.
CONNECT_TIMEOUT_SECONDS = 5.0


class TestKVServerFixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)

    def test_the_server_serves_on_the_socket_the_harness_bound(self):
        kv = KVServer(self.tmp_path)
        self.addCleanup(kv.stop)
        # The server got the harness's bound socket by descriptor, not a port
        # number to bind for itself, so there was no pick-then-bind window.
        self.assertIn("--fd", kv.process.args)
        self.assertNotIn("--port", kv.process.args)
        status, _ = http_json(f"{kv.url}/healthz", token=None)
        self.assertEqual(status, 200)
        with socket.socket() as other, self.assertRaises(OSError) as caught:
            other.bind((LOOPBACK_HOST, kv.port))
        self.assertEqual(caught.exception.errno, errno.EADDRINUSE)
        # The harness let go of its copy once the server answered, so a server
        # that dies mid-test leaves no listening socket behind it: the next
        # request is refused at once rather than waiting out its timeout.
        self.assertEqual(kv.socket.fileno(), -1)
        kv.process.kill()
        kv.process.wait()
        with self.assertRaises(ConnectionRefusedError):
            socket.create_connection((LOOPBACK_HOST, kv.port), timeout=CONNECT_TIMEOUT_SECONDS)

    def test_a_start_that_times_out_names_the_port_and_releases_it(self):
        # A zero budget fails before the first poll, so this never races the
        # server. The port it names is held by the harness's socket until
        # `stop()` closes it, after the server process has been waited on; a
        # port that binds again is the record that both have happened.
        with self.assertRaises(AssertionError) as caught:
            KVServer(self.tmp_path, startup_timeout=0)
        message = str(caught.exception)
        named = PORT_IN_MESSAGE.search(message)
        self.assertIsNotNone(named, message)
        self.assertIn("(last poll: not polled)", message)
        self.assertIn(LOG_HEADING, message)
        with socket.socket() as reclaim:
            reclaim.bind((LOOPBACK_HOST, int(named.group(1))))

    def test_a_server_that_exits_early_reports_its_status_and_log(self):
        # A PYTHONHOME that holds no standard library stops the interpreter
        # before uvicorn runs, so the server exits without serving, and
        # `_healthy` reports the exit on its first poll rather than waiting
        # out the budget. (PYTHONPATH cannot stand in for this: uvicorn puts
        # its working directory, the scripts directory, on sys.path itself.)
        with self.assertRaises(AssertionError) as caught:
            KVServer(self.tmp_path, env={"PYTHONHOME": str(self.tmp_path / "no-python")})
        message = str(caught.exception)
        self.assertRegex(message, r"session_kv_server on :\d+ exited early with status [1-9]")
        self.assertIn(LOG_HEADING, message)
        self.assertIn("Fatal Python error", message.split(LOG_HEADING)[1])


if __name__ == "__main__":
    unittest.main()
