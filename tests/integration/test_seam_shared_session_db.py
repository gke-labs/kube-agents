"""Seam: the session_kv.db file, shared by the KV server and the chat plugins.

One SQLite file, several writers in different processes: the KV server (WAL,
subprocess here as in the pod), the chat gateway's session_store plugin, and
readers in the console. Each piece is unit-tested on its own temporary
database; nothing before this file ever opened the same one from two sides at
once. The two assertions that matter: concurrent writers must not surface
"database is locked", and the server's plaintext-identity purge must strip
exactly the identity field while leaving the routing fields other writers own.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from _seams import KVServer, REPO_ROOT, http_json

PLUGINS_DIR = REPO_ROOT / "agents" / "chat" / "defaults" / "plugins"
sys.path.insert(0, str(PLUGINS_DIR))
sys.path.insert(0, str(PLUGINS_DIR / "session_store"))


class SharedSessionDBTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        self.kv = KVServer(self.tmp_path)
        self.addCleanup(self.kv.stop)

        # The plugin reads its path from the environment at call time.
        import os

        os.environ["SESSION_KV_DB_PATH"] = self.kv.db_path
        self.addCleanup(os.environ.pop, "SESSION_KV_DB_PATH", None)

        sys.modules.pop("store", None)
        import store

        store._Connection._close_unlocked() if hasattr(store, "_Connection") else None
        self.store = store

    def test_interleaved_writers_never_see_database_is_locked(self):
        errors = []

        def plugin_writer():
            try:
                for i in range(25):
                    self.store.write_session_metadata(
                        f"chat-session-{i}",
                        {
                            "session_id": f"chat-session-{i}",
                            "platform": "google_chat",
                            "chat_id": "spaces/AAA",
                            "thread_id": f"spaces/AAA/threads/t{i}",
                        },
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def server_writer():
            try:
                for _ in range(25):
                    status, _ = http_json(
                        f"{self.kv.url}/sessions", payload={}, method="POST"
                    )
                    assert status == 201
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=plugin_writer),
            threading.Thread(target=server_writer),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual([], errors)
        with sqlite3.connect(self.kv.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM session_metadata"
            ).fetchone()[0]
        self.assertGreaterEqual(count, 50)

    def test_the_identity_purge_strips_email_but_keeps_the_thread_routing(self):
        # A row written the pre-pseudonymisation way, directly — the shape the
        # purge exists for.
        with sqlite3.connect(self.kv.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO session_metadata (session_id, metadata) VALUES (?, ?)",
                (
                    "legacy-session",
                    json.dumps(
                        {
                            "platform": "google_chat",
                            "user_email": "someone@example.com",
                            "chat_id": "spaces/AAA",
                            "thread_id": "spaces/AAA/threads/t1",
                        }
                    ),
                ),
            )

        # The purge runs at server startup; restart the server on the same DB.
        self.kv.stop()
        self.kv = KVServer(self.tmp_path)
        self.addCleanup(self.kv.stop)

        status, meta = http_json(f"{self.kv.url}/v1/sessions/legacy-session/metadata")
        self.assertEqual(200, status)
        self.assertNotIn("user_email", meta)
        self.assertEqual("spaces/AAA/threads/t1", meta["thread_id"])
        self.assertEqual("spaces/AAA", meta["chat_id"])


if __name__ == "__main__":
    unittest.main()
