"""Unit tests for event-triage chat routing installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import ast
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import kanban_event_routing as ker
from apply_kanban_event_routing import ANCHOR, RELATIVE, apply
from kanban_event_routing import (
    DB_PATH_ENV,
    DEFAULT_DB_PATH,
    NON_CHAT_ORIGINS,
    resolve_chat_route,
    session_kv_db_path,
)

SPACE = "spaces/0EXAMPLE"
THREAD = "spaces/0EXAMPLE/threads/ALERT1"

SCHEMA = """
CREATE TABLE session_metadata (
    session_id TEXT PRIMARY KEY,
    metadata TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def routing_db(path, rows):
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        for session_id, metadata in rows.items():
            conn.execute(
                "INSERT INTO session_metadata (session_id, metadata) VALUES (?, ?)",
                (session_id, json.dumps(metadata)),
            )
    return str(path)


class ResolveChatRouteTest(unittest.TestCase):
    """The substitution itself."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = routing_db(
            self.tmp / "session_kv.db",
            {
                "k8s-evt-routed": {
                    "platform": "google_chat",
                    "chat_id": SPACE,
                    "thread_id": THREAD,
                },
                "k8s-evt-threadless": {
                    "platform": "google_chat",
                    "chat_id": SPACE,
                },
                "k8s-evt-prefix": {"platform": "k8s-watcher"},
                "k8s-evt-channelless": {"platform": "google_chat"},
                SPACE: {"platform": "slack", "chat_id": "C0WRONG"},
            },
        )

    def resolve(self, platform, chat_id, thread_id=None):
        return resolve_chat_route(platform, chat_id, thread_id, db_path=self.db)

    def test_event_session_is_readdressed_to_the_alert_thread(self):
        self.assertEqual(
            self.resolve("api_server", "k8s-evt-routed"),
            ("google_chat", SPACE, THREAD),
        )

    def test_watcher_origin_is_readdressed_too(self):
        # The platform event sessions carried before the watcher recorded the
        # real one. It is no more deliverable than api_server.
        self.assertEqual(
            self.resolve("k8s-watcher", "k8s-evt-routed"),
            ("google_chat", SPACE, THREAD),
        )

    def test_a_recorded_route_without_a_thread_still_delivers(self):
        # Verified live: with no thread the report lands in the space rather
        # than under the alert. Degraded, not lost.
        self.assertEqual(
            self.resolve("api_server", "k8s-evt-threadless"),
            ("google_chat", SPACE, None),
        )

    def test_an_existing_thread_is_not_carried_across_the_substitution(self):
        # The incoming thread belongs to the api_server session, not to the
        # chat route being substituted in; keeping it would address the reply
        # to a thread in the wrong channel.
        self.assertEqual(
            self.resolve("api_server", "k8s-evt-threadless", "stale-thread"),
            ("google_chat", SPACE, None),
        )

    def test_chat_sessions_are_left_alone(self):
        # Even though SPACE keys a row that a lookup would happily return.
        self.assertEqual(
            self.resolve("google_chat", SPACE, THREAD),
            ("google_chat", SPACE, THREAD),
        )

    def test_tui_sessions_are_left_alone(self):
        self.assertEqual(
            self.resolve("tui", "agent:main:tui:x"), ("tui", "agent:main:tui:x", None)
        )

    def test_a_stored_route_that_is_also_non_chat_is_refused(self):
        self.assertEqual(
            self.resolve("api_server", "k8s-evt-prefix"),
            ("api_server", "k8s-evt-prefix", None),
        )

    def test_a_stored_route_without_a_channel_is_refused(self):
        self.assertEqual(
            self.resolve("api_server", "k8s-evt-channelless"),
            ("api_server", "k8s-evt-channelless", None),
        )

    def test_an_unknown_session_falls_through(self):
        self.assertEqual(
            self.resolve("api_server", "k8s-evt-absent"),
            ("api_server", "k8s-evt-absent", None),
        )

    def test_an_empty_chat_id_falls_through(self):
        self.assertEqual(self.resolve("api_server", ""), ("api_server", "", None))


class FailOpenTest(unittest.TestCase):
    """`kanban_tools` runs where none of this exists. It must not notice."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_a_missing_database_falls_through(self):
        self.assertEqual(
            resolve_chat_route(
                "api_server", "k8s-evt-x", None, db_path=str(self.tmp / "absent.db")
            ),
            ("api_server", "k8s-evt-x", None),
        )

    def test_a_corrupt_database_falls_through(self):
        corrupt = self.tmp / "corrupt.db"
        corrupt.write_bytes(b"this is not a sqlite database")
        self.assertEqual(
            resolve_chat_route("api_server", "k8s-evt-x", None, db_path=str(corrupt)),
            ("api_server", "k8s-evt-x", None),
        )

    def test_malformed_metadata_json_falls_through(self):
        path = self.tmp / "bad.db"
        with sqlite3.connect(path) as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT INTO session_metadata (session_id, metadata) VALUES (?, ?)",
                ("k8s-evt-x", "{not json"),
            )
        self.assertEqual(
            resolve_chat_route("api_server", "k8s-evt-x", None, db_path=str(path)),
            ("api_server", "k8s-evt-x", None),
        )

    def test_metadata_that_is_not_an_object_falls_through(self):
        path = self.tmp / "list.db"
        with sqlite3.connect(path) as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT INTO session_metadata (session_id, metadata) VALUES (?, ?)",
                ("k8s-evt-x", json.dumps(["google_chat"])),
            )
        self.assertEqual(
            resolve_chat_route("api_server", "k8s-evt-x", None, db_path=str(path)),
            ("api_server", "k8s-evt-x", None),
        )

    def test_a_missing_table_falls_through(self):
        path = self.tmp / "empty.db"
        sqlite3.connect(path).close()
        self.assertEqual(
            resolve_chat_route("api_server", "k8s-evt-x", None, db_path=str(path)),
            ("api_server", "k8s-evt-x", None),
        )

    def test_the_database_is_never_created_by_a_lookup(self):
        missing = self.tmp / "never.db"
        resolve_chat_route("api_server", "k8s-evt-x", None, db_path=str(missing))
        self.assertFalse(missing.exists())


class UndeliverableWarningTest(unittest.TestCase):
    """Failing open on an event session is the original bug, so it is loud.

    The substitution cannot be made mandatory — a CLI worker has no routing
    database and is not doing triage — so nothing distinguishes a legitimate
    fall-through from a report about to be dropped except this line.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_an_unrecorded_session_says_the_report_will_not_arrive(self):
        db = routing_db(self.tmp / "empty.db", {})
        with self.assertLogs(ker.log, level="WARNING") as caught:
            resolve_chat_route("api_server", "k8s-evt-x", None, db_path=db)
        self.assertIn("k8s-evt-x", caught.output[0])
        self.assertIn("will not reach chat", caught.output[0])

    def test_a_non_chat_stored_route_names_what_it_found(self):
        db = routing_db(self.tmp / "pre.db", {"k8s-evt-x": {"platform": "k8s-watcher"}})
        with self.assertLogs(ker.log, level="WARNING") as caught:
            resolve_chat_route("api_server", "k8s-evt-x", None, db_path=db)
        self.assertIn("k8s-watcher", caught.output[0])

    def test_an_unreadable_database_names_the_error(self):
        corrupt = self.tmp / "corrupt.db"
        corrupt.write_bytes(b"this is not a sqlite database")
        with self.assertLogs(ker.log, level="WARNING") as caught:
            resolve_chat_route("api_server", "k8s-evt-x", None, db_path=str(corrupt))
        self.assertIn("could not be read", caught.output[0])

    def test_a_successful_substitution_is_silent(self):
        db = routing_db(
            self.tmp / "ok.db",
            {"k8s-evt-x": {"platform": "google_chat", "chat_id": SPACE}},
        )
        with mock.patch.object(ker.log, "warning") as warned:
            resolve_chat_route("api_server", "k8s-evt-x", None, db_path=db)
        warned.assert_not_called()

    def test_a_cli_worker_is_silent(self):
        # Not an event session, so there is nothing to warn about. Otherwise
        # every kanban_create on a laptop with no routing database warns.
        with mock.patch.object(ker.log, "warning") as warned:
            resolve_chat_route(
                "tui", "agent:main:tui:x", None, db_path=str(self.tmp / "absent.db")
            )
        warned.assert_not_called()


class DbPathTest(unittest.TestCase):
    def test_the_env_override_wins(self):
        with mock.patch.dict("os.environ", {DB_PATH_ENV: "/tmp/elsewhere.db"}):
            self.assertEqual(session_kv_db_path(), "/tmp/elsewhere.db")

    def test_the_default_matches_the_session_kv_server(self):
        # Kept in step with agents/platform/scripts/session_kv_server.py, which
        # owns the file this reads.
        self.assertEqual(DEFAULT_DB_PATH, "/var/lib/kube-agents/session/session_kv.db")

    def test_both_undeliverable_origins_are_covered(self):
        self.assertEqual(NON_CHAT_ORIGINS, frozenset({"api_server", "k8s-watcher"}))


class ApplyTest(unittest.TestCase):
    """The anchored edit."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.target = self.root / RELATIVE
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.target.write_text(
            "def _maybe_auto_subscribe(conn, tid):\n"
            "    try:\n"
            "        platform = get_session_env('HERMES_SESSION_PLATFORM', '')\n"
            "        chat_id = get_session_env('HERMES_SESSION_CHAT_ID', '')\n"
            + ANCHOR
            + "        return (platform, chat_id, thread_id)\n"
            "    except Exception:\n"
            "        return False\n"
        )

    def test_the_edit_lands_and_still_parses(self):
        apply(self.root)
        patched = self.target.read_text()
        self.assertIn("_kanban_event_route(", patched)
        self.assertIn("from tools.kanban_event_routing import", patched)
        ast.parse(patched)

    def test_the_call_lands_after_thread_id_is_read(self):
        apply(self.root)
        patched = self.target.read_text()
        self.assertLess(
            patched.index('thread_id = get_session_env("HERMES_SESSION_THREAD_ID"'),
            patched.index("platform, chat_id, thread_id = _kanban_event_route("),
        )

    def test_a_second_run_is_refused(self):
        apply(self.root)
        with self.assertRaises(SystemExit):
            apply(self.root)

    def test_a_tree_without_the_anchor_is_refused(self):
        self.target.write_text("def _maybe_auto_subscribe(conn, tid):\n    return False\n")
        with self.assertRaises(SystemExit):
            apply(self.root)


if __name__ == "__main__":
    unittest.main()
