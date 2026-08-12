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
    _READ_SCRIPT,
    AgentRuntimeProvider,
    KubeCommandResult,
)
from admin_console.project_config import DeploymentTarget


class AgentRuntimeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, arguments: list[str], *, timeout: int = 20) -> KubeCommandResult:
        self.calls.append(arguments)
        if (
            "pods" in arguments
            and "kubeagents.x-k8s.io/has-credential-proxy=true" in arguments
        ):
            return KubeCommandResult(
                0,
                json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {
                                    "name": "test-agent-01-gateway-1",
                                    "labels": {"app": "test-agent-01-gateway"},
                                }
                            }
                        ]
                    }
                ),
            )
        if "pods" in arguments:
            return KubeCommandResult(
                0,
                json.dumps(
                    {"items": [{"metadata": {"name": "test-agent-01-gateway-1"}}]}
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


class LegacyGatewayRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, arguments: list[str], *, timeout: int = 20) -> KubeCommandResult:
        self.calls.append(arguments)
        if "platformagents" in arguments:
            return KubeCommandResult(
                0,
                json.dumps(
                    {"items": [{"metadata": {"name": "platform-agent"}}]}
                ),
            )
        if "kubeagents.x-k8s.io/has-credential-proxy=true" in arguments:
            return KubeCommandResult(0, json.dumps({"items": []}))
        return KubeCommandResult(
            0,
            json.dumps(
                {
                    "items": [
                        {
                            "metadata": {
                                "name": "platform-agent-gateway-1",
                                "labels": {"app": "platform-agent-gateway"},
                            }
                        }
                    ]
                }
            ),
        )


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

    def _run_script(self, root: Path, *arguments: str) -> dict:
        completed = subprocess.run(
            [sys.executable, "-c", self._script_for(root), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(completed.stdout)

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
        self.assertEqual(self.provider.list_agents(), ("test-agent-01",))
        result = self.provider.list_conversations(
            "test-agent-01", cutoff=datetime(2023, 1, 1, tzinfo=UTC)
        )

        conversation = result.conversations[0]
        self.assertEqual(conversation.user, "user@example.com")
        self.assertEqual(conversation.platform, "google_chat")
        self.assertIn("[REDACTED]", conversation.preview)
        self.assertNotIn("secret-value", conversation.preview)

    def test_discovers_legacy_gateway_through_bounded_platformagent_fallback(self):
        runner = LegacyGatewayRunner()
        provider = AgentRuntimeProvider(self.provider.target, runner=runner)

        self.assertEqual(provider.list_agents(), ("platform-agent",))
        fallback = runner.calls[-1]
        self.assertIn("app in (platform-agent-gateway)", fallback)

    def test_reads_only_user_and_assistant_projection(self):
        result = self.provider.get_messages(
            "test-agent-01", profile="default", session_id="session-1"
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

    def test_connection_probe_returns_only_counts(self):
        self.assertEqual(self.provider.check_connection("test-agent-01"), (2, 5))

    def test_reads_linked_agent_work_and_redacts_summary(self):
        result = self.provider.get_task_updates(
            "test-agent-01",
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
        result = self.provider.list_kanban_tasks("test-agent-01")

        self.assertEqual(len(result.tasks), 1)
        task = result.tasks[0]
        self.assertEqual(task.task_id, "t_12345678")
        self.assertEqual(task.run_count, 1)
        self.assertEqual(task.child_count, 1)

    def test_reads_kanban_task_detail_and_redacts_evidence(self):
        detail = self.provider.get_kanban_task("test-agent-01", "t_12345678")

        self.assertEqual(detail.task.status, "done")
        self.assertEqual(detail.task.run_count, 101)
        self.assertTrue(detail.runs_truncated)
        self.assertEqual(detail.runs[0].outcome, "completed")
        self.assertEqual(detail.children[0].task_id, "t_87654321")
        self.assertEqual(detail.deliveries[0].platform, "tui")
        self.assertIn("[REDACTED]", detail.body)
        self.assertIn("[REDACTED]", detail.events[0].payload)

    def test_reads_cron_jobs_executions_and_scheduler_health(self):
        snapshot = self.provider.get_cron_snapshot("test-agent-01")

        self.assertEqual(snapshot.jobs[0].name, "unique-visitors")
        self.assertEqual(snapshot.jobs[0].scheduler, "missing")
        self.assertEqual(snapshot.jobs[0].schedule_kind, "interval")
        self.assertEqual(snapshot.jobs[0].interval_minutes, 60)
        self.assertIn("[REDACTED]", snapshot.jobs[0].task)
        self.assertEqual(snapshot.executions[0].source, "direct")
        self.assertEqual(snapshot.executions[0].status, "completed")
        exec_call = next(call for call in self.runner.calls if "cron" in call)
        self.assertNotIn("sh", exec_call)


if __name__ == "__main__":
    unittest.main()
