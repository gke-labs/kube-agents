from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from admin_console.agent_runtime import (
    _READ_FRAME_TAG,
    _READ_SCRIPT,
    AgentRuntimeError,
    AgentRuntimeProvider,
    KubeCommandResult,
)
from admin_console.kanban_view import display_status
from admin_console.project_config import DeploymentTarget

# A result that cites a status page and a log path. It does not link a PR.
CITED_RESULT = (
    "Checked https://status.cloud.google.com — all green.\n"
    "Rotated logs under /mnt/pull/2024-10-01."
)


class AgentRuntimeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, arguments: list[str], *, timeout: int = 20) -> KubeCommandResult:
        self.calls.append(arguments)
        if "platformagents" in arguments:
            return KubeCommandResult(
                0,
                json.dumps({"items": [{"metadata": {"name": "platform-agent"}}]}),
            )
        if "pods" in arguments:
            return KubeCommandResult(
                0,
                json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {"name": "platform-agent-gateway-1"},
                                "spec": {
                                    "containers": [
                                        {
                                            "name": "runtime-from-pod",
                                            "ports": [
                                                {
                                                    "name": "api",
                                                    "containerPort": 8642,
                                                }
                                            ],
                                        },
                                        {"name": "credential-proxy"},
                                    ]
                                },
                            }
                        ]
                    }
                ),
            )
        if "conversations" in arguments:
            return KubeCommandResult(
                0,
                json.dumps(
                    {
                        "conversations": [
                            {
                                "id": "session-1",
                                "profile": "default",
                                "platform": "google_chat",
                                "user": "user@example.com",
                                "attribution": "attributed",
                                "preview": "api_key=secret-value",
                                "chat_type": "dm",
                                "started_at": 1_700_000_000,
                                "last_active": 1_700_000_010,
                                "chat_message_count": 2,
                                "tool_call_count": 1,
                            }
                        ],
                        "truncated": False,
                    }
                ),
            )
        if "messages" in arguments:
            return KubeCommandResult(
                0,
                json.dumps(
                    {
                        "messages": [
                            {
                                "id": 1,
                                "role": "user",
                                "content": "password=hunter2",
                                "timestamp": 1_700_000_000,
                            },
                            {
                                "id": 2,
                                "role": "assistant",
                                "content": "Done",
                                "timestamp": 1_700_000_010,
                            },
                        ],
                        "truncated": False,
                    }
                ),
            )
        if "tasks" in arguments:
            return KubeCommandResult(
                0,
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "t_12345678",
                                "title": "Inspect applications",
                                "assignee": "platform",
                                "status": "done",
                                "created_at": 1_700_000_000,
                                "updated_at": 1_700_000_010,
                                "summary": "Found password=hunter2",
                                "error": "",
                                "run_count": 2,
                                "latest_event": "heartbeat",
                                "latest_event_at": 1_700_000_011,
                                "previous_error": "first run crashed",
                            }
                        ],
                        "truncated": False,
                    }
                ),
            )
        if "board" in arguments:
            return KubeCommandResult(
                0,
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "t_12345678",
                                "title": "Inspect applications",
                                "assignee": "platform",
                                "status": "done",
                                "priority": 1,
                                "created_at": 1_700_000_000,
                                "updated_at": 1_700_000_010,
                                "session_id": "portal_session_1",
                                "run_count": 1,
                                "parent_count": 0,
                                "child_count": 1,
                                "summary": "Found applications",
                                "error": "",
                            }
                        ],
                        "truncated": False,
                    }
                ),
            )
        if "cron" in arguments:
            return KubeCommandResult(
                0,
                json.dumps(
                    {
                        "jobs": [
                            {
                                "profile": "cluster-test-01",
                                "id": "job-1",
                                "name": "unique-visitors",
                                "enabled": True,
                                "state": "scheduled",
                                "cadence": "every 60m",
                                "prompt": "Count api_key=secret-value",
                                "script": "count.py",
                                "mode": "script",
                                "last_run_at": "2026-08-05T18:00:00+00:00",
                                "next_run_at": "2026-08-05T19:00:00+00:00",
                                "last_status": "ok",
                                "scheduler": "missing",
                                "schedule_kind": "interval",
                                "schedule_expression": "",
                                "interval_minutes": 60,
                            }
                        ],
                        "executions": [
                            {
                                "id": "execution-1",
                                "profile": "cluster-test-01",
                                "job_id": "job-1",
                                "source": "direct",
                                "status": "completed",
                                "claimed_at": "2026-08-05T18:00:00+00:00",
                                "started_at": "2026-08-05T18:00:01+00:00",
                                "finished_at": "2026-08-05T18:00:03+00:00",
                                "error": "",
                            }
                        ],
                        "jobs_truncated": False,
                        "executions_truncated": False,
                        "read_at": 1_754_418_010,
                    }
                ),
            )
        if "task" in arguments and "t_12345678" in arguments:
            return KubeCommandResult(
                0,
                json.dumps(
                    {
                        "task": {
                            "id": "t_12345678",
                            "title": "Inspect applications",
                            "body": "List password=hunter2",
                            "assignee": "platform",
                            "status": "done",
                            "priority": 1,
                            "created_by": "worker",
                            "created_at": 1_700_000_000,
                            "started_at": 1_700_000_001,
                            "completed_at": 1_700_000_010,
                            "updated_at": 1_700_000_010,
                            "workspace_kind": "scratch",
                            "project_id": "test-project-01",
                            "result": "done",
                            "session_id": "portal_session_1",
                            "consecutive_failures": 0,
                            "goal_mode": 0,
                            "total_run_count": 101,
                        },
                        "parents": [],
                        "children": [
                            {
                                "id": "t_87654321",
                                "title": "Child",
                                "assignee": "cluster-one",
                                "status": "done",
                            }
                        ],
                        "runs": [
                            {
                                "id": 4,
                                "profile": "platform",
                                "status": "done",
                                "started_at": 1_700_000_001,
                                "ended_at": 1_700_000_010,
                                "outcome": "completed",
                                "summary": "Found applications",
                                "metadata": "{\"count\": 3}",
                                "error": "",
                            }
                        ],
                        "runs_truncated": True,
                        "events": [
                            {
                                "id": 8,
                                "run_id": 4,
                                "kind": "completed",
                                "payload": "{\"secret\": \"api_key=value\"}",
                                "created_at": 1_700_000_010,
                            }
                        ],
                        "comments": [],
                        "attachments": [],
                        "deliveries": [
                            {
                                "platform": "tui",
                                "has_chat_id": True,
                                "has_thread_id": False,
                                "has_user_id": False,
                                "created_at": 1_700_000_000,
                                "last_event_id": 0,
                            }
                        ],
                    }
                ),
            )
        if "health" in arguments:
            return KubeCommandResult(
                0,
                json.dumps(
                    {
                        "profiles": [
                            {"profile": "default", "sessions": 2},
                            {"profile": "platform", "sessions": 3},
                        ]
                    }
                ),
            )
        return KubeCommandResult(1, stderr="unexpected command")


class NonCanonicalAgentRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, arguments: list[str], *, timeout: int = 20) -> KubeCommandResult:
        self.calls.append(arguments)
        if "platformagents" in arguments:
            return KubeCommandResult(
                0,
                json.dumps(
                    {
                        "items": [
                            {"metadata": {"name": "ux-e2e"}},
                            {"metadata": {"name": "custom-agent"}},
                        ]
                    }
                ),
            )
        return KubeCommandResult(1, stderr="unexpected command")


class EmbeddedReadScriptTest(unittest.TestCase):
    def _script_for(self, root: Path) -> str:
        replacements = {
            'Path("/opt/data/state.db")': f"Path({str(root / 'state.db')!r})",
            'Path("/opt/data/profiles")': f"Path({str(root / 'profiles')!r})",
            'Path("/opt/data/kanban.db")': f"Path({str(root / 'kanban.db')!r})",
            'Path("/var/lib/kube-agents/session/session_kv.db")': (
                f"Path({str(root / 'session_kv.db')!r})"
            ),
        }
        script = _READ_SCRIPT
        for original, replacement in replacements.items():
            script = script.replace(original, replacement)
        return script

    def _run_script(self, root: Path, *arguments: str, prelude: str = "") -> dict:
        completed = subprocess.run(
            [sys.executable, "-c", prelude + self._script_for(root), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
        header, body = completed.stdout.split("\n", 1)
        self.assertEqual(header, f"{_READ_FRAME_TAG} {len(body)}")
        return json.loads(body)

    def _create_board_db(self, path: Path, rows: list[tuple[str, str, str]]) -> None:
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
                    priority INTEGER, session_id TEXT, created_at REAL,
                    started_at REAL, completed_at REAL, last_heartbeat_at REAL,
                    last_failure_error TEXT
                );
                CREATE TABLE task_runs (
                    id INTEGER PRIMARY KEY, task_id TEXT, summary TEXT, error TEXT
                );
                CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
                """
            )
            for index, (task_id, title, summary) in enumerate(rows, start=1):
                connection.execute(
                    "INSERT INTO tasks VALUES (?, ?, 'platform', 'done', 0, '', "
                    "?, NULL, ?, NULL, '')",
                    (task_id, title, index, index),
                )
                connection.execute(
                    "INSERT INTO task_runs (task_id, summary, error) "
                    "VALUES (?, ?, '')",
                    (task_id, summary),
                )

    def test_script_and_console_share_the_frame_tag(self):
        self.assertIn(f'FRAME_TAG = "{_READ_FRAME_TAG}"', _READ_SCRIPT)

    def test_board_over_64_kib_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._create_board_db(
                root / "kanban.db",
                [(f"t_{index:08d}", f"Task {index}", "s" * 1024) for index in range(500)],
            )
            payload = self._run_script(root, "board", "500")

        self.assertEqual(len(payload["tasks"]), 500)
        self.assertEqual(payload["tasks"][0]["summary"], "s" * 1024)

    def test_board_survives_short_writes(self):
        prelude = (
            "import os\n"
            "_write = os.write\n"
            "os.write = lambda fd, data: _write(fd, bytes(data[:1000]))\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._create_board_db(
                root / "kanban.db",
                [(f"t_{index:08d}", f"Task {index}", "s" * 1024) for index in range(100)],
            )
            payload = self._run_script(root, "board", "500", prelude=prelude)

        self.assertEqual(len(payload["tasks"]), 100)

    def test_board_writes_at_most_64_kib_per_call(self):
        prelude = (
            "import os, sys\n"
            "_write = os.write\n"
            "def _logged(fd, data):\n"
            "    sys.stderr.write(f'{len(data)}\\n')\n"
            "    return _write(fd, data)\n"
            "os.write = _logged\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._create_board_db(
                root / "kanban.db",
                [(f"t_{index:08d}", f"Task {index}", "s" * 1024) for index in range(500)],
            )
            completed = subprocess.run(
                [sys.executable, "-c", prelude + self._script_for(root), "board", "500"],
                check=True,
                capture_output=True,
                text=True,
            )

        sizes = [int(line) for line in completed.stderr.split()]
        self.assertGreater(len(sizes), 1)
        self.assertLessEqual(max(sizes), 65536)
        self.assertGreater(sum(sizes), 65536)

    def test_board_holds_after_its_last_write(self):
        prelude = (
            "import os, sys, time\n"
            "_write = os.write\n"
            "def _logged(fd, data):\n"
            "    sys.stderr.write('write\\n')\n"
            "    return _write(fd, data)\n"
            "os.write = _logged\n"
            "time.sleep = lambda seconds: sys.stderr.write(f'sleep {seconds}\\n')\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._create_board_db(root / "kanban.db", [("t_00000001", "Task", "s")])
            completed = subprocess.run(
                [sys.executable, "-c", prelude + self._script_for(root), "board", "500"],
                check=True,
                capture_output=True,
                text=True,
            )

        self.assertEqual(completed.stderr.splitlines()[-1], "sleep 0.25")

    def test_board_round_trips_non_ascii_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._create_board_db(
                root / "kanban.db", [("t_00000001", "节点池 — rollout ✓", "résumé")]
            )
            payload = self._run_script(root, "board", "500")

        self.assertEqual(payload["tasks"][0]["title"], "节点池 — rollout ✓")
        self.assertEqual(payload["tasks"][0]["summary"], "résumé")

    def _create_history_db(
        self, path: Path, sessions: list[tuple[str, float]]
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY, source TEXT, user_id TEXT,
                    display_name TEXT, chat_id TEXT, chat_type TEXT,
                    thread_id TEXT, title TEXT, started_at REAL, ended_at REAL,
                    message_count INTEGER, tool_call_count INTEGER
                );
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
                    content TEXT, timestamp REAL, active INTEGER
                );
                """
            )
            for index, (session_id, timestamp) in enumerate(sessions, start=1):
                connection.execute(
                    "INSERT INTO sessions VALUES (?, 'test-chat-surface-01', '', '', '', '', '', '', ?, NULL, 2, 0)",
                    (session_id, timestamp),
                )
                connection.execute(
                    "INSERT INTO messages VALUES (?, ?, 'user', ?, ?, 1)",
                    (index, session_id, f"prompt-{session_id}", timestamp),
                )

    def test_conversations_bound_each_profile_then_merge_latest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._create_history_db(
                root / "state.db",
                [("default-new", 30), ("default-mid", 20), ("default-old", 10)],
            )
            self._create_history_db(
                root / "profiles" / "platform" / "state.db",
                [("platform-new", 40), ("platform-mid", 25), ("platform-old", 1)],
            )
            with closing(sqlite3.connect(root / "session_kv.db")) as connection, connection:
                connection.execute(
                    "CREATE TABLE session_metadata (session_id TEXT PRIMARY KEY, metadata TEXT)"
                )
                connection.executemany(
                    "INSERT INTO session_metadata VALUES (?, ?)",
                    [
                        (
                            "default-new",
                            json.dumps(
                                {"platform": "admin_portal", "user_email": "d@example.com"}
                            ),
                        ),
                        (
                            "platform-new",
                            json.dumps(
                                {"platform": "google_chat", "user_email": "p@example.com"}
                            ),
                        ),
                    ],
                )

            payload = self._run_script(root, "conversations", "15", "2")

        self.assertEqual(
            [row["id"] for row in payload["conversations"]],
            ["platform-new", "default-new"],
        )
        self.assertEqual(
            [row["user"] for row in payload["conversations"]],
            ["p@example.com", "d@example.com"],
        )
        self.assertTrue(payload["truncated"])

    def test_messages_report_truncation_only_when_an_extra_row_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._create_history_db(root / "state.db", [("session-1", 1)])
            with closing(sqlite3.connect(root / "state.db")) as connection, connection:
                connection.execute("DELETE FROM messages")
                connection.executemany(
                    "INSERT INTO messages VALUES (?, 'session-1', 'user', ?, ?, 1)",
                    [(index, f"message-{index}", index) for index in range(1, 101)],
                )

            exact = self._run_script(
                root,
                "messages",
                "default",
                "session-1",
                "100",
            )
            with closing(sqlite3.connect(root / "state.db")) as connection, connection:
                connection.execute(
                    "INSERT INTO messages VALUES (101, 'session-1', 'assistant', "
                    "'message-101', 101, 1)"
                )
            overflow = self._run_script(
                root,
                "messages",
                "default",
                "session-1",
                "100",
            )

        self.assertEqual(len(exact["messages"]), 100)
        self.assertFalse(exact["truncated"])
        self.assertEqual(len(overflow["messages"]), 100)
        self.assertTrue(overflow["truncated"])

    def test_tasks_report_truncation_only_when_an_extra_row_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with closing(sqlite3.connect(root / "kanban.db")) as connection, connection:
                connection.executescript(
                    """
                    CREATE TABLE tasks (
                        id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
                        session_id TEXT, created_at REAL, started_at REAL,
                        completed_at REAL, last_heartbeat_at REAL,
                        last_failure_error TEXT
                    );
                    CREATE TABLE task_runs (
                        id INTEGER PRIMARY KEY, task_id TEXT, summary TEXT, error TEXT
                    );
                    CREATE TABLE task_events (
                        id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, created_at REAL
                    );
                    """
                )
                connection.executemany(
                    "INSERT INTO tasks VALUES (?, ?, 'platform', 'done', "
                    "'portal_session', ?, NULL, ?, NULL, '')",
                    [
                        (f"task-{index:03d}", f"Task {index}", index, index)
                        for index in range(1, 101)
                    ],
                )

            exact = self._run_script(root, "tasks", "portal_session", "100")
            with closing(sqlite3.connect(root / "kanban.db")) as connection, connection:
                connection.execute(
                    "INSERT INTO tasks VALUES ('task-101', 'Task 101', 'platform', "
                    "'done', 'portal_session', 101, NULL, 101, NULL, '')"
                )
            overflow = self._run_script(root, "tasks", "portal_session", "100")

        self.assertEqual(len(exact["tasks"]), 100)
        self.assertFalse(exact["truncated"])
        self.assertEqual(len(overflow["tasks"]), 100)
        self.assertTrue(overflow["truncated"])

    def test_task_detail_returns_newest_runs_and_total_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with closing(sqlite3.connect(root / "kanban.db")) as connection, connection:
                connection.executescript(
                    """
                    CREATE TABLE tasks (
                        id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
                        created_at REAL, started_at REAL, completed_at REAL,
                        last_heartbeat_at REAL
                    );
                    CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
                    CREATE TABLE task_runs (
                        id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT,
                        step_key TEXT, status TEXT, started_at REAL, ended_at REAL,
                        outcome TEXT, summary TEXT, metadata TEXT, error TEXT
                    );
                    CREATE TABLE task_events (
                        id INTEGER, task_id TEXT, run_id INTEGER, kind TEXT,
                        payload TEXT, created_at REAL
                    );
                    CREATE TABLE task_comments (
                        id INTEGER, task_id TEXT, author TEXT, body TEXT,
                        created_at REAL
                    );
                    CREATE TABLE task_attachments (
                        id INTEGER, task_id TEXT, filename TEXT, content_type TEXT,
                        size INTEGER, uploaded_by TEXT, created_at REAL
                    );
                    CREATE TABLE kanban_notify_subs (
                        task_id TEXT, platform TEXT, chat_id TEXT, thread_id TEXT,
                        user_id TEXT, notifier_profile TEXT, created_at REAL,
                        last_event_id INTEGER
                    );
                    INSERT INTO tasks VALUES (
                        't_12345678', 'Task', 'platform', 'done', 1, 2, 3, 3
                    );
                    """
                )
                connection.executemany(
                    "INSERT INTO task_runs VALUES (?, 't_12345678', 'platform', '', 'done', ?, ?, 'completed', '', '', '')",
                    [(run_id, run_id, run_id) for run_id in range(1, 106)],
                )

            payload = self._run_script(root, "task", "t_12345678")

        self.assertEqual(payload["task"]["total_run_count"], 105)
        self.assertTrue(payload["runs_truncated"])
        self.assertEqual(payload["runs"][0]["id"], 6)
        self.assertEqual(payload["runs"][-1]["id"], 105)

    def _create_card_state_board(self, path: Path) -> None:
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
                    priority INTEGER, session_id TEXT, created_at REAL,
                    started_at REAL, completed_at REAL, last_heartbeat_at REAL,
                    last_failure_error TEXT, block_kind TEXT, result TEXT
                );
                CREATE TABLE task_runs (
                    id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT,
                    step_key TEXT, status TEXT, started_at REAL, ended_at REAL,
                    outcome TEXT, summary TEXT, metadata TEXT, error TEXT
                );
                CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
                CREATE TABLE task_events (
                    id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER,
                    kind TEXT, payload TEXT, created_at REAL
                );
                CREATE TABLE task_comments (
                    id INTEGER, task_id TEXT, author TEXT, body TEXT,
                    created_at REAL
                );
                CREATE TABLE task_attachments (
                    id INTEGER, task_id TEXT, filename TEXT, content_type TEXT,
                    size INTEGER, uploaded_by TEXT, created_at REAL
                );
                CREATE TABLE kanban_notify_subs (
                    task_id TEXT, platform TEXT, chat_id TEXT, thread_id TEXT,
                    user_id TEXT, notifier_profile TEXT, created_at REAL,
                    last_event_id INTEGER
                );
                """
            )
            cards = [
                ("t_done_pr", "done", None, None),
                ("t_done", "done", None, "No change was needed."),
                ("t_done_bad", "done", None, None),
                (
                    "t_result_pr",
                    "done",
                    None,
                    "Opened https://github.com/acme/infra/pull/12 for review.",
                ),
                (
                    "t_result_mr",
                    "done",
                    None,
                    "Opened https://gitlab.example/acme/infra/-/merge_requests/4.",
                ),
                (
                    "t_result_issue",
                    "done",
                    None,
                    "Filed https://github.com/acme/infra/issues/9.",
                ),
                # Two URL-like parts with white space between them are not
                # one PR URL.
                (
                    "t_result_cited",
                    "done",
                    None,
                    CITED_RESULT,
                ),
                (
                    "t_result_spaced",
                    "done",
                    None,
                    "See https://status.cloud.google.com and /mnt/pull/2024-10-01.",
                ),
                # A link that only offers to open a PR has no PR number.
                (
                    "t_result_new_pr",
                    "done",
                    None,
                    "Open https://github.com/acme/infra/pull/new/fix-dns to file it.",
                ),
                # The console shows a PR only for a done card.
                (
                    "t_running_pr",
                    "running",
                    None,
                    "Opened https://github.com/acme/infra/pull/13 for review.",
                ),
                ("t_pr_false", "done", None, None),
                ("t_pr_zero", "done", None, None),
                ("t_input", "blocked", "needs_input", None),
                ("t_access", "blocked", "capability", None),
                ("t_gave_up", "blocked", "needs_input", None),
                ("t_unblocked", "blocked", "needs_input", None),
                ("t_triage", "triage", None, None),
            ]
            connection.executemany(
                "INSERT INTO tasks VALUES (?, 'Card', 'platform', ?, 0, "
                "'portal_session', ?, NULL, NULL, NULL, '', ?, ?)",
                [
                    (task_id, status, index, kind, result)
                    for index, (task_id, status, kind, result) in enumerate(
                        cards, 1
                    )
                ],
            )
            connection.executemany(
                "INSERT INTO task_runs (task_id, status, metadata) VALUES (?, 'done', ?)",
                [
                    # A later review run does not hide the URL of an older run.
                    ("t_done_pr", '{"pr_url": "https://example.invalid/pull/7"}'),
                    ("t_done_pr", '{"decision": "approve"}'),
                    ("t_done_pr", None),
                    ("t_done", '{"pr_url": ""}'),
                    ("t_done", '{"decision": "no change"}'),
                    ("t_done_bad", "not json"),
                    # Only a text value is a URL.
                    ("t_pr_false", '{"pr_url": false}'),
                    ("t_pr_zero", '{"pr_url": 0}'),
                ],
            )
            connection.executemany(
                "INSERT INTO task_events (task_id, kind, payload, created_at) "
                "VALUES (?, ?, '{}', 0)",
                [
                    ("t_input", "blocked"),
                    ("t_access", "blocked"),
                    # A block, an unblock, then the breaker. The breaker keeps
                    # the old kind, and a later comment is the newest event.
                    ("t_gave_up", "blocked"),
                    ("t_gave_up", "unblocked"),
                    ("t_gave_up", "gave_up"),
                    ("t_gave_up", "commented"),
                    # A person unblocked the card after the breaker. A later
                    # status move without a new 'blocked' event is not a give up.
                    ("t_unblocked", "gave_up"),
                    ("t_unblocked", "unblocked"),
                    ("t_unblocked", "status"),
                    ("t_triage", "blocked"),
                    ("t_triage", "blocked"),
                ],
            )

    def test_reads_select_block_kind_gave_up_and_pr(self):
        expected = {
            "t_done_pr": (None, 0, 1),
            "t_done": (None, 0, 0),
            "t_done_bad": (None, 0, 0),
            "t_result_pr": (None, 0, 1),
            "t_result_mr": (None, 0, 1),
            "t_result_issue": (None, 0, 0),
            "t_result_cited": (None, 0, 0),
            "t_result_spaced": (None, 0, 0),
            "t_result_new_pr": (None, 0, 0),
            "t_running_pr": (None, 0, 0),
            "t_pr_false": (None, 0, 0),
            "t_pr_zero": (None, 0, 0),
            "t_input": ("needs_input", 0, 0),
            "t_access": ("capability", 0, 0),
            "t_gave_up": ("needs_input", 1, 0),
            "t_unblocked": ("needs_input", 0, 0),
            "t_triage": (None, 0, 0),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._create_card_state_board(root / "kanban.db")
            with closing(sqlite3.connect(root / "kanban.db")) as connection, connection:
                connection.executemany(
                    "INSERT INTO task_links VALUES (?, ?)",
                    [
                        ("t_result_pr", "t_gave_up"),
                        ("t_gave_up", "t_result_mr"),
                        ("t_gave_up", "t_result_cited"),
                    ],
                )
            board = self._run_script(root, "board", "100")
            tasks = self._run_script(root, "tasks", "portal_session", "100")
            detail = self._run_script(root, "task", "t_gave_up")

        for read, payload in (("board", board), ("tasks", tasks)):
            with self.subTest(read=read):
                self.assertEqual(
                    {
                        row["id"]: (row["block_kind"], row["gave_up"], row["has_pr"])
                        for row in payload["tasks"]
                    },
                    expected,
                )
        self.assertEqual(
            (
                detail["task"]["block_kind"],
                detail["task"]["gave_up"],
                detail["task"]["has_pr"],
            ),
            ("needs_input", 1, 0),
        )
        self.assertEqual(
            [(row["id"], row["has_pr"]) for row in detail["parents"]],
            [("t_result_pr", True)],
        )
        self.assertEqual(
            [(row["id"], row["has_pr"]) for row in detail["children"]],
            [("t_result_mr", True), ("t_result_cited", False)],
        )
        # The read scans the result but does not send it.
        for payload in (
            board["tasks"],
            tasks["tasks"],
            detail["parents"],
            detail["children"],
        ):
            text = json.dumps(payload)
            self.assertNotIn("status.cloud.google.com", text)
            self.assertNotIn("github.com/acme/infra/pull", text)
            self.assertNotIn("merge_requests", text)
            self.assertNotIn("pr_scan_result", text)

    def test_reads_tolerate_a_board_without_card_state_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._create_board_db(root / "kanban.db", [("t_00000001", "Task", "s")])
            board = self._run_script(root, "board", "10")

        row = board["tasks"][0]
        self.assertEqual(
            (row["block_kind"], row["gave_up"], row["has_pr"]), (None, 0, 0)
        )
        self.assertNotIn("pr_scan_result", row)

    def test_provider_parses_card_state_from_the_real_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._create_card_state_board(root / "kanban.db")
            with closing(sqlite3.connect(root / "kanban.db")) as connection, connection:
                connection.execute(
                    "INSERT INTO task_links VALUES ('t_done_pr', 't_gave_up')"
                )

            def provider_for(*arguments: str) -> AgentRuntimeProvider:
                completed = subprocess.run(
                    [sys.executable, "-c", self._script_for(root), *arguments],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return AgentRuntimeProvider(
                    DeploymentTarget("test-project-01", "test-cluster-01", "us-east4"),
                    runner=FramedReadRunner(completed.stdout),
                )

            board = provider_for("board", "100").list_kanban_tasks("platform-agent")
            updates = provider_for("tasks", "portal_session", "100").get_task_updates(
                "platform-agent", session_id="portal_session"
            )
            detail = provider_for("task", "t_done_pr").get_kanban_task(
                "platform-agent", "t_done_pr"
            )

        for result in (board, updates):
            cards = {task.task_id: task for task in result.tasks}
            self.assertEqual(
                (cards["t_gave_up"].block_kind, cards["t_gave_up"].gave_up),
                ("needs_input", True),
            )
            self.assertEqual(
                (cards["t_input"].block_kind, cards["t_input"].gave_up),
                ("needs_input", False),
            )
            self.assertTrue(cards["t_done_pr"].has_pr)
            self.assertFalse(cards["t_done"].has_pr)
            self.assertEqual(cards["t_done"].block_kind, "")
        self.assertTrue(detail.task.has_pr)
        self.assertFalse(detail.task.gave_up)
        self.assertEqual(
            [(child.task_id, display_status(child)) for child in detail.children],
            [("t_gave_up", "failed after all attempts")],
        )


class AgentRuntimeProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = AgentRuntimeRunner()
        self.provider = AgentRuntimeProvider(
            DeploymentTarget(
                "test-project-01",
                "test-cluster-01",
                "us-east4",
            ),
            runner=self.runner,
        )

    def test_reads_real_conversation_metadata_and_redacts_preview(self):
        self.assertEqual(self.provider.list_agents(), ("platform-agent",))
        result = self.provider.list_conversations(
            "platform-agent", cutoff=datetime(2023, 1, 1, tzinfo=UTC)
        )

        conversation = result.conversations[0]
        self.assertEqual(conversation.user, "user@example.com")
        self.assertEqual(conversation.platform, "google_chat")
        self.assertIn("[REDACTED]", conversation.preview)
        self.assertNotIn("secret-value", conversation.preview)

    def test_missing_canonical_agent_lists_discovered_resources(self):
        runner = NonCanonicalAgentRunner()
        provider = AgentRuntimeProvider(self.provider.target, runner=runner)

        with self.assertRaises(AgentRuntimeError) as caught:
            provider.canonical_agent()

        message = str(caught.exception)
        self.assertIn("platform-agent", message)
        self.assertIn("custom-agent, ux-e2e", message)
        self.assertNotIn("test-cluster-01", message)

    def test_reads_only_user_and_assistant_projection(self):
        result = self.provider.get_messages(
            "platform-agent", profile="default", session_id="session-1"
        )

        self.assertEqual(
            [message.role for message in result.messages], ["user", "assistant"]
        )
        self.assertIn("[REDACTED]", result.messages[0].content)
        exec_call = next(call for call in self.runner.calls if "messages" in call)
        self.assertEqual(
            exec_call[0:2],
            [
                "--context",
                "gke_test-project-01_us-east4_test-cluster-01",
            ],
        )
        self.assertNotIn("sh", exec_call)
        self.assertEqual(exec_call[exec_call.index("-c") + 1], "runtime-from-pod")

    def test_connection_probe_returns_only_counts(self):
        self.assertEqual(self.provider.check_connection("platform-agent"), (2, 5))

    def test_reads_linked_agent_work_and_redacts_summary(self):
        result = self.provider.get_task_updates(
            "platform-agent",
            session_id="portal_session_1",
        )

        self.assertEqual(len(result.tasks), 1)
        task = result.tasks[0]
        self.assertEqual(task.task_id, "t_12345678")
        self.assertEqual(task.status, "done")
        self.assertEqual(task.run_count, 2)
        self.assertEqual(task.latest_event, "heartbeat")
        self.assertEqual(task.previous_error, "first run crashed")
        self.assertIn("[REDACTED]", task.summary)
        self.assertNotIn("hunter2", task.summary)

    def test_reads_bounded_kanban_board(self):
        result = self.provider.list_kanban_tasks("platform-agent")

        self.assertEqual(len(result.tasks), 1)
        task = result.tasks[0]
        self.assertEqual(task.task_id, "t_12345678")
        self.assertEqual(task.run_count, 1)
        self.assertEqual(task.child_count, 1)

    def test_reads_kanban_task_detail_and_redacts_evidence(self):
        detail = self.provider.get_kanban_task("platform-agent", "t_12345678")

        self.assertEqual(detail.task.status, "done")
        self.assertEqual(detail.task.run_count, 101)
        self.assertTrue(detail.runs_truncated)
        self.assertEqual(detail.runs[0].outcome, "completed")
        self.assertEqual(detail.children[0].task_id, "t_87654321")
        self.assertEqual(detail.deliveries[0].platform, "tui")
        self.assertIn("[REDACTED]", detail.body)
        self.assertIn("[REDACTED]", detail.events[0].payload)

    def test_reads_cron_jobs_executions_and_scheduler_health(self):
        snapshot = self.provider.get_cron_snapshot("platform-agent")

        self.assertEqual(snapshot.jobs[0].name, "unique-visitors")
        self.assertEqual(snapshot.jobs[0].scheduler, "missing")
        self.assertEqual(snapshot.jobs[0].schedule_kind, "interval")
        self.assertEqual(snapshot.jobs[0].interval_minutes, 60)
        self.assertIn("[REDACTED]", snapshot.jobs[0].task)
        self.assertEqual(snapshot.executions[0].source, "direct")
        self.assertEqual(snapshot.executions[0].status, "completed")
        exec_call = next(call for call in self.runner.calls if "cron" in call)
        self.assertNotIn("sh", exec_call)


class FramedReadRunner(AgentRuntimeRunner):
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        super().__init__()
        self.stdout = stdout
        self.returncode = returncode

    def run(self, arguments: list[str], *, timeout: int = 20) -> KubeCommandResult:
        if "exec" in arguments:
            self.calls.append(arguments)
            return KubeCommandResult(self.returncode, self.stdout)
        return super().run(arguments, timeout=timeout)


class FramedReadTest(unittest.TestCase):
    def _provider(self, runner: AgentRuntimeRunner) -> AgentRuntimeProvider:
        return AgentRuntimeProvider(
            DeploymentTarget("test-project-01", "test-cluster-01", "us-east4"),
            runner=runner,
        )

    def test_complete_framed_read_parses(self):
        body = json.dumps({"profiles": [{"profile": "default", "sessions": 4}]})
        provider = self._provider(
            FramedReadRunner(f"{_READ_FRAME_TAG} {len(body)}\n{body}")
        )

        self.assertEqual(provider.check_connection("platform-agent"), (1, 4))

    def test_truncated_read_reports_cut_short(self):
        for returncode in (0, 1):
            with self.subTest(returncode=returncode):
                provider = self._provider(
                    FramedReadRunner(
                        f"{_READ_FRAME_TAG} 200000\n" + "a" * 65536, returncode
                    )
                )
                with self.assertRaises(AgentRuntimeError) as raised:
                    provider.list_kanban_tasks("platform-agent")

                self.assertEqual(
                    str(raised.exception),
                    "Agent runtime read was cut short: received 65536 of 200000 bytes.",
                )
                self.assertIn("gVisor", raised.exception.guidance)

    def test_read_longer_than_its_header_is_invalid(self):
        provider = self._provider(FramedReadRunner(f"{_READ_FRAME_TAG} 2\n{{}}{{}}"))

        with self.assertRaisesRegex(AgentRuntimeError, "returned invalid data"):
            provider.check_connection("platform-agent")

    def test_header_length_int_refuses_is_invalid(self):
        for length in ("²", "9" * 5000):
            with self.subTest(length=length[:8]):
                provider = self._provider(
                    FramedReadRunner(f"{_READ_FRAME_TAG} {length}\n{{}}")
                )
                with self.assertRaisesRegex(AgentRuntimeError, "returned invalid data"):
                    provider.check_connection("platform-agent")

    def test_failed_read_without_a_header_still_reports_failure(self):
        provider = self._provider(FramedReadRunner("", returncode=1))

        with self.assertRaisesRegex(AgentRuntimeError, "Agent runtime read failed"):
            provider.check_connection("platform-agent")


if __name__ == "__main__":
    unittest.main()
