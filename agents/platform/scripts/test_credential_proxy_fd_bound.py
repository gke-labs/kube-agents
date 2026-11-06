"""The agent API door (`:8643`) holds a constant number of open files during a streamed turn.

`AgentAPIProxyHandler` checks a request's session id against every profile's
`state.db` so an external caller cannot continue a bridge-owned session. The
check opens one read-only SQLite connection per profile. Until the fix for
#4330 the connections stayed open until the request ended: through the upstream
read loop that relays a streamed turn (SSE), which can run for minutes. With `N`
cluster profiles, one streamed turn held `N + 1` descriptors for its whole
length, and a fleet of a few hundred clusters met `EMFILE` mid-stream. The door
now closes every handle before the upstream turn starts.

The test below is the gate that keeps it so: it creates `PROFILE_COUNT` profile
databases, starts a streamed turn whose upstream holds after its first chunk,
and counts `/proc/self/fd` while the stream is open. The count may exceed the
idle count by the request's own sockets, never by anything that grows with
`PROFILE_COUNT`. It lives in its own file so changes to the door's own tests in
`test_credential_proxy.py` and this gate do not collide.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from credential_proxy import AgentAPIProxyHandler

# The issue's fleet size: enough that a per-profile handle is unmistakable
# against the request's own sockets, and well under a default 1024 soft limit.
PROFILE_COUNT = 500
# How far above the idle count the mid-stream count may sit: the client socket,
# the door's accepted socket, its upstream connection and the fake upstream's
# accepted socket, with room for a listener's spare. Independent of PROFILE_COUNT.
FD_DELTA_CEILING = 10
FD_DIR = Path("/proc/self/fd")
EXTERNAL_KEY = "external-key-for-test"
UPSTREAM_KEY = "upstream-key-for-test"
PLAIN_SESSION_ID = "plain-parent"
FIRST_CHUNK = b'event: run.started\ndata: {"seq": 1}\n\n'
SECOND_CHUNK = b"event: done\ndata: {}\n\n"
RELEASE_TIMEOUT_S = 30
REQUEST_TIMEOUT_S = 30
STATE_DB_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS sessions ("
    "id TEXT PRIMARY KEY, parent_session_id TEXT, end_reason TEXT, model_config TEXT)"
)


def _open_fd_count() -> int:
    return len(os.listdir(FD_DIR))


def _chunk(payload: bytes) -> bytes:
    return f"{len(payload):x}\r\n".encode("ascii") + payload + b"\r\n"


@unittest.skipUnless(FD_DIR.is_dir(), "needs /proc/self/fd to count open descriptors")
class AgentAPIProxyFdBoundTest(unittest.TestCase):
    def setUp(self):
        owner = self

        class HoldingUpstreamHandler(BaseHTTPRequestHandler):
            """Streams two SSE chunks and holds between them until the test says go."""

            protocol_version = "HTTP/1.1"

            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.wfile.write(_chunk(FIRST_CHUNK))
                self.wfile.flush()
                owner.second_chunk_released = owner.release_second_chunk.wait(timeout=RELEASE_TIMEOUT_S)
                self.wfile.write(_chunk(SECOND_CHUNK) + b"0\r\n\r\n")
                self.wfile.flush()

            def log_message(self, _message, *_args):
                return

        self.release_second_chunk = threading.Event()
        self.second_chunk_released = False
        self.workspace_dir = tempfile.TemporaryDirectory()
        self._seed_profiles(Path(self.workspace_dir.name))
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), HoldingUpstreamHandler)
        self.saved = {
            name: getattr(AgentAPIProxyHandler, name)
            for name in ("external_key", "upstream_key", "upstream_port", "workspace_root")
            if hasattr(AgentAPIProxyHandler, name)
        }
        AgentAPIProxyHandler.external_key = EXTERNAL_KEY
        AgentAPIProxyHandler.upstream_key = UPSTREAM_KEY
        AgentAPIProxyHandler.upstream_port = self.upstream.server_port
        AgentAPIProxyHandler.workspace_root = self.workspace_dir.name
        self.proxy = ThreadingHTTPServer(("127.0.0.1", 0), AgentAPIProxyHandler)
        for server in (self.upstream, self.proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.release_second_chunk.set()
        self.proxy.shutdown()
        self.upstream.shutdown()
        self.proxy.server_close()
        self.upstream.server_close()
        for name, value in self.saved.items():
            setattr(AgentAPIProxyHandler, name, value)
        self.workspace_dir.cleanup()

    @staticmethod
    def _seed_profiles(root: Path) -> None:
        """The root state.db plus PROFILE_COUNT profile databases, each with a sessions table."""
        paths = [root / "state.db"] + [
            root / "profiles" / f"cluster-{index:03d}" / "state.db" for index in range(PROFILE_COUNT)
        ]
        for db_path in paths:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.closing(sqlite3.connect(db_path)) as conn:
                conn.execute(STATE_DB_SCHEMA)
                conn.commit()

    def test_the_door_sees_every_profile(self):
        """The fixture is what the door reads: PROFILE_COUNT + 1 databases, so the gate below has its signal."""
        self.assertEqual(
            PROFILE_COUNT + 1,
            len(AgentAPIProxyHandler._bridge_session_dbs(self.workspace_dir.name)),
        )

    def _stream_one_turn(self) -> tuple[int, int]:
        """Run one streamed turn through the door and return (idle, mid-stream) descriptor counts.

        Every step but the bound is asserted here, so a request the door refuses
        or a stream it truncates fails the round-trip test on its own line rather
        than reading as a descriptor count.
        """
        idle = _open_fd_count()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.proxy.server_port}/api/sessions/{PLAIN_SESSION_ID}/chat/stream",
            headers={"Authorization": f"Bearer {EXTERNAL_KEY}", "X-Hermes-Session-Id": PLAIN_SESSION_ID},
        )
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
            self.assertEqual(FIRST_CHUNK.splitlines(keepends=True)[0], response.readline())
            mid_stream = _open_fd_count()
            self.release_second_chunk.set()
            rest = response.read()
        self.assertTrue(self.second_chunk_released)
        self.assertIn(SECOND_CHUNK, rest)
        return idle, mid_stream

    def test_a_streamed_turn_round_trips_with_every_profile_seeded(self):
        """The request the bound test makes is accepted and streamed to its end."""
        idle, mid_stream = self._stream_one_turn()
        self.assertGreater(mid_stream, idle)

    # The bound #4330's fix restored: the door opens one connection per profile
    # for the session-id check and closes them before the upstream read loop, so
    # the mid-stream count sits a few sockets above idle, not PROFILE_COUNT. A
    # handle held through the stream again puts this PROFILE_COUNT above idle.
    def test_open_fds_during_a_stream_do_not_grow_with_the_profile_count(self):
        idle, mid_stream = self._stream_one_turn()
        self.assertLess(
            mid_stream - idle,
            FD_DELTA_CEILING,
            f"{mid_stream - idle} descriptors opened for one streamed turn across {PROFILE_COUNT} profiles: "
            "the door holds a state.db handle per profile through the stream",
        )


if __name__ == "__main__":
    unittest.main()
