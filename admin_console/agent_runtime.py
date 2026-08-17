"""Bounded access to persisted agent conversations and runtime state in GKE."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from admin_console.kube_access import (
    GKEKubeAccess,
    KubeCommandResult,
    kube_failure_guidance,
)
from admin_console.project_config import (
    DeploymentTarget,
    is_valid_cluster_name,
    is_valid_location,
    is_valid_namespace,
    is_valid_project_id,
)
from admin_console.telemetry import redact_evidence

_K8S_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_KANBAN_TASK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_READ_SCRIPT = r'''
import json
import sqlite3
import sys
import time
from pathlib import Path


def profiles():
    found = {"default": Path("/opt/data/state.db")}
    root = Path("/opt/data/profiles")
    if root.is_dir():
        for path in root.glob("*/state.db"):
            try:
                if path.resolve().is_relative_to(root.resolve()):
                    found[path.parent.name] = path
            except OSError:
                pass
    return {name: path for name, path in found.items() if path.is_file()}


def connect(path):
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def metadata(session_ids):
    if not session_ids:
        return {}
    path = Path("/var/lib/kube-agents/session/session_kv.db")
    if not path.is_file():
        return {}
    result = {}
    placeholders = ",".join("?" for _ in session_ids)
    with connect(path) as connection:
        for row in connection.execute(
            f"SELECT session_id, metadata FROM session_metadata "
            f"WHERE session_id IN ({placeholders})",
            tuple(session_ids),
        ):
            try:
                result[row["session_id"]] = json.loads(row["metadata"])
            except (TypeError, json.JSONDecodeError):
                pass
    return result


def health():
    rows = []
    for name, path in profiles().items():
        with connect(path) as connection:
            count = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        rows.append({"profile": name, "sessions": count})
    return {"profiles": rows}


def conversations(cutoff, limit):
    rows = []
    truncated = False
    query = """
        WITH candidates AS (
            SELECT s.id, s.source, s.user_id, s.display_name, s.chat_id,
                   s.chat_type, s.thread_id, s.title, s.started_at, s.ended_at,
                   s.message_count, s.tool_call_count,
                   COALESCE(
                       (SELECT MAX(m.timestamp) FROM messages m
                        WHERE m.session_id = s.id AND COALESCE(m.active, 1) = 1),
                       s.started_at
                   ) AS last_active,
                   (SELECT COUNT(*) FROM messages m
                    WHERE m.session_id = s.id AND COALESCE(m.active, 1) = 1
                      AND m.role IN ('user', 'assistant')) AS chat_message_count,
                   (SELECT SUBSTR(m.content, 1, 180) FROM messages m
                    WHERE m.session_id = s.id AND COALESCE(m.active, 1) = 1
                      AND m.role = 'user'
                    ORDER BY m.timestamp LIMIT 1) AS preview
            FROM sessions s
            WHERE EXISTS (
                SELECT 1 FROM messages m WHERE m.session_id = s.id
                  AND COALESCE(m.active, 1) = 1
                  AND m.role IN ('user', 'assistant')
            )
        )
        SELECT * FROM candidates
        WHERE CAST(last_active AS REAL) >= ?
        ORDER BY CAST(last_active AS REAL) DESC
        LIMIT ?
    """
    for profile, path in profiles().items():
        with connect(path) as connection:
            profile_rows = list(connection.execute(query, (cutoff, limit + 1)))
            if len(profile_rows) > limit:
                truncated = True
            for raw in profile_rows[:limit]:
                row = dict(raw)
                row["profile"] = profile
                rows.append(row)
    rows.sort(key=lambda item: float(item.get("last_active") or 0), reverse=True)
    truncated = truncated or len(rows) > limit
    rows = rows[:limit]
    identities = metadata({row["id"] for row in rows})
    for row in rows:
        meta = identities.get(row["id"], {})
        identity = (
            meta.get("user_email")
            or meta.get("user_id")
            or row.get("user_id")
            or ""
        )
        row.update({
            "platform": meta.get("platform") or row.get("source") or "unknown",
            "user": identity,
            "attribution": "attributed" if identity else "unattributed",
        })
    return {"conversations": rows, "truncated": truncated}


def messages(profile, session_id, limit):
    path = profiles().get(profile)
    if path is None:
        raise ValueError("Unknown profile")
    with connect(path) as connection:
        exists = connection.execute(
            "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not exists:
            raise ValueError("Unknown session")
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, role, content, timestamp
                FROM messages
                WHERE session_id = ? AND COALESCE(active, 1) = 1
                  AND role IN ('user', 'assistant')
                ORDER BY timestamp, id
                LIMIT ?
                """,
                (session_id, limit + 1),
            )
        ]
    return {"messages": rows[:limit], "truncated": len(rows) > limit}


def tasks(session_id, limit):
    path = Path("/opt/data/kanban.db")
    if not path.is_file():
        return {"tasks": [], "truncated": False}
    with connect(path) as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT t.id, t.title, t.assignee, t.status, t.created_at,
                       COALESCE(
                           t.completed_at,
                           t.last_heartbeat_at,
                           t.started_at,
                           t.created_at
                       ) AS updated_at,
                       (SELECT r.summary FROM task_runs r
                        WHERE r.task_id = t.id
                        ORDER BY r.id DESC LIMIT 1) AS summary,
                       COALESCE(
                           (SELECT r.error FROM task_runs r
                            WHERE r.task_id = t.id
                            ORDER BY r.id DESC LIMIT 1),
                           t.last_failure_error
                       ) AS error,
                       (SELECT COUNT(*) FROM task_runs r
                        WHERE r.task_id = t.id) AS run_count,
                       (SELECT e.kind FROM task_events e
                        WHERE e.task_id = t.id
                        ORDER BY e.id DESC LIMIT 1) AS latest_event,
                       (SELECT e.created_at FROM task_events e
                        WHERE e.task_id = t.id
                        ORDER BY e.id DESC LIMIT 1) AS latest_event_at,
                       COALESCE(
                           (SELECT r.error FROM task_runs r
                            WHERE r.task_id = t.id
                              AND r.error IS NOT NULL AND r.error <> ''
                            ORDER BY r.id DESC LIMIT 1),
                           t.last_failure_error
                       ) AS previous_error
                FROM tasks t
                WHERE t.session_id = ?
                ORDER BY t.created_at, t.id
                LIMIT ?
                """,
                (session_id, limit + 1),
            )
        ]
    return {"tasks": rows[:limit], "truncated": len(rows) > limit}


def board(limit):
    path = Path("/opt/data/kanban.db")
    if not path.is_file():
        return {"tasks": [], "truncated": False}
    with connect(path) as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT t.id, t.title, t.assignee, t.status, t.priority,
                       t.created_at,
                       COALESCE(
                           t.completed_at,
                           t.last_heartbeat_at,
                           t.started_at,
                           t.created_at
                       ) AS updated_at,
                       t.session_id,
                       (SELECT COUNT(*) FROM task_runs r
                        WHERE r.task_id = t.id) AS run_count,
                       (SELECT COUNT(*) FROM task_links l
                        WHERE l.child_id = t.id) AS parent_count,
                       (SELECT COUNT(*) FROM task_links l
                        WHERE l.parent_id = t.id) AS child_count,
                       (SELECT r.summary FROM task_runs r
                        WHERE r.task_id = t.id
                        ORDER BY r.id DESC LIMIT 1) AS summary,
                       COALESCE(
                           (SELECT r.error FROM task_runs r
                            WHERE r.task_id = t.id
                            ORDER BY r.id DESC LIMIT 1),
                           t.last_failure_error
                       ) AS error
                FROM tasks t
                ORDER BY updated_at DESC, t.id
                LIMIT ?
                """,
                (limit + 1,),
            )
        ]
    return {"tasks": rows[:limit], "truncated": len(rows) > limit}


def task_detail(task_id):
    path = Path("/opt/data/kanban.db")
    if not path.is_file():
        raise ValueError("Task Kanban unavailable")
    with connect(path) as connection:
        task = connection.execute(
            """
            SELECT t.*,
                   (SELECT COUNT(*) FROM task_runs r
                    WHERE r.task_id = t.id) AS total_run_count,
                   COALESCE(
                       t.completed_at,
                       t.last_heartbeat_at,
                       t.started_at,
                       t.created_at
                   ) AS updated_at
            FROM tasks t WHERE t.id = ?
            """,
            (task_id,),
        ).fetchone()
        if task is None:
            raise ValueError("Unknown task")

        def related(column, match_column):
            return [
                dict(row)
                for row in connection.execute(
                    f"""
                    SELECT t.id, t.title, t.assignee, t.status
                    FROM task_links l JOIN tasks t ON t.id = l.{column}
                    WHERE l.{match_column} = ? ORDER BY t.created_at, t.id
                    """,
                    (task_id,),
                )
            ]

        runs = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, profile, step_key, status, started_at, ended_at,
                       outcome, summary, metadata, error
                FROM task_runs WHERE task_id = ? ORDER BY id DESC
                LIMIT 100
                """,
                (task_id,),
            )
        ]
        runs.reverse()
        events = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, run_id, kind, payload, created_at
                FROM task_events WHERE task_id = ? ORDER BY id
                LIMIT 500
                """,
                (task_id,),
            )
        ]
        comments = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, author, body, created_at
                FROM task_comments WHERE task_id = ? ORDER BY id
                LIMIT 200
                """,
                (task_id,),
            )
        ]
        attachments = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, filename, content_type, size, uploaded_by, created_at
                FROM task_attachments WHERE task_id = ? ORDER BY id
                LIMIT 100
                """,
                (task_id,),
            )
        ]
        deliveries = [
            {
                "platform": row["platform"],
                "has_chat_id": bool(row["chat_id"]),
                "has_thread_id": bool(row["thread_id"]),
                "has_user_id": bool(row["user_id"]),
                "notifier_profile": row["notifier_profile"],
                "created_at": row["created_at"],
                "last_event_id": row["last_event_id"],
            }
            for row in connection.execute(
                """
                SELECT platform, chat_id, thread_id, user_id,
                       notifier_profile, created_at, last_event_id
                FROM kanban_notify_subs WHERE task_id = ?
                ORDER BY created_at
                """,
                (task_id,),
            )
        ]
        parents = related("parent_id", "child_id")
        children = related("child_id", "parent_id")
    return {
        "task": dict(task),
        "parents": parents,
        "children": children,
        "runs": runs,
        "runs_truncated": int(task["total_run_count"] or 0) > len(runs),
        "events": events,
        "comments": comments,
        "attachments": attachments,
        "deliveries": deliveries,
    }


def cron_snapshot(job_limit, execution_limit):
    cron_roots = [("default", Path("/opt/data/cron"))]
    profiles_root = Path("/opt/data/profiles")
    if profiles_root.is_dir():
        for jobs_path in sorted(profiles_root.glob("*/cron/jobs.json"))[:100]:
            try:
                if jobs_path.resolve().is_relative_to(profiles_root.resolve()):
                    cron_roots.append((jobs_path.parent.parent.name, jobs_path.parent))
            except OSError:
                pass

    now = time.time()
    jobs = []
    executions = []
    for profile, cron_dir in cron_roots:
        heartbeat = cron_dir / "ticker_heartbeat"
        heartbeat_at = heartbeat.stat().st_mtime if heartbeat.is_file() else None
        if heartbeat_at is None:
            scheduler = "missing"
        elif now - heartbeat_at <= 180:
            scheduler = "active"
        else:
            scheduler = "stale"

        jobs_path = cron_dir / "jobs.json"
        if jobs_path.is_file():
            try:
                payload = json.loads(jobs_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            raw_jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
            for job in raw_jobs:
                if not isinstance(job, dict):
                    continue
                schedule = job.get("schedule") or {}
                cadence = job.get("schedule_display") or schedule.get("display") or ""
                if not cadence and schedule.get("kind") == "cron":
                    cadence = schedule.get("expr") or schedule.get("expression") or "cron"
                elif not cadence and schedule.get("kind") == "interval":
                    cadence = f"every {schedule.get('minutes')}m"
                jobs.append({
                    "profile": profile,
                    "id": job.get("id"),
                    "name": job.get("name"),
                    "enabled": bool(job.get("enabled")),
                    "state": job.get("state"),
                    "cadence": cadence,
                    "prompt": job.get("prompt"),
                    "script": job.get("script"),
                    "mode": "script" if job.get("no_agent") else "agent",
                    "last_run_at": job.get("last_run_at"),
                    "next_run_at": job.get("next_run_at"),
                    "last_status": job.get("last_status"),
                    "last_error": job.get("last_error"),
                    "scheduler": scheduler,
                    "heartbeat_at": heartbeat_at,
                    "schedule_kind": schedule.get("kind"),
                    "schedule_expression": schedule.get("expr") or schedule.get("expression"),
                    "interval_minutes": schedule.get("minutes"),
                })

        executions_path = cron_dir / "executions.db"
        if not executions_path.is_file():
            continue
        try:
            with connect(executions_path) as connection:
                table = connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'executions'"
                ).fetchone()
                if not table:
                    continue
                rows = connection.execute(
                    """
                    SELECT id, job_id, source, status, claimed_at,
                           started_at, finished_at, error
                    FROM executions
                    ORDER BY COALESCE(started_at, claimed_at) DESC
                    LIMIT ?
                    """,
                    (execution_limit + 1,),
                )
                for row in rows:
                    item = dict(row)
                    item["profile"] = profile
                    executions.append(item)
        except (OSError, sqlite3.DatabaseError):
            pass

    jobs.sort(key=lambda item: (item["profile"], str(item.get("name") or "")))
    executions.sort(
        key=lambda item: str(item.get("started_at") or item.get("claimed_at") or ""),
        reverse=True,
    )
    return {
        "jobs": jobs[:job_limit],
        "executions": executions[:execution_limit],
        "jobs_truncated": len(jobs) > job_limit,
        "executions_truncated": len(executions) > execution_limit,
        "read_at": now,
    }


action = sys.argv[1]
if action == "health":
    payload = health()
elif action == "conversations":
    payload = conversations(float(sys.argv[2]), int(sys.argv[3]))
elif action == "messages":
    payload = messages(sys.argv[2], sys.argv[3], int(sys.argv[4]))
elif action == "tasks":
    payload = tasks(sys.argv[2], int(sys.argv[3]))
elif action == "board":
    payload = board(int(sys.argv[2]))
elif action == "task":
    payload = task_detail(sys.argv[2])
elif action == "cron":
    payload = cron_snapshot(int(sys.argv[2]), int(sys.argv[3]))
else:
    raise ValueError("Unsupported action")
print(json.dumps(payload, ensure_ascii=False))
'''


class KubeRunner(Protocol):
    def run(self, arguments: list[str], *, timeout: int = 20) -> KubeCommandResult: ...


class KubectlRunner:
    """Runtime adapter over the portal's shared GKE access component."""

    def __init__(
        self,
        target: DeploymentTarget,
        *,
        access: GKEKubeAccess | None = None,
    ) -> None:
        self.access = access or GKEKubeAccess(target)

    def run(self, arguments: list[str], *, timeout: int = 20) -> KubeCommandResult:
        return self.access.run(arguments, timeout=timeout)


class AgentRuntimeError(RuntimeError):
    """Safe user-facing history read failure."""

    def __init__(self, message: str, guidance: str = "") -> None:
        super().__init__(message)
        self.guidance = guidance


@dataclass(frozen=True)
class AgentConversation:
    session_id: str
    profile: str
    platform: str
    user: str
    attribution: str
    title: str
    preview: str
    chat_type: str
    chat_id: str
    thread_id: str
    started_at: datetime
    last_active: datetime
    message_count: int
    tool_call_count: int

    @property
    def user_key(self) -> str:
        value = self.user or "unattributed"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class AgentMessage:
    message_id: int
    role: str
    content: str
    occurred_at: datetime


@dataclass(frozen=True)
class HistoryResult:
    conversations: tuple[AgentConversation, ...]
    truncated: bool


@dataclass(frozen=True)
class MessageResult:
    messages: tuple[AgentMessage, ...]
    truncated: bool


@dataclass(frozen=True)
class AgentTaskUpdate:
    task_id: str
    title: str
    assignee: str
    status: str
    created_at: datetime
    updated_at: datetime
    summary: str
    error: str
    run_count: int = 0
    latest_event: str = ""
    latest_event_at: datetime | None = None
    previous_error: str = ""


@dataclass(frozen=True)
class TaskUpdateResult:
    tasks: tuple[AgentTaskUpdate, ...]
    truncated: bool


@dataclass(frozen=True)
class KanbanTaskSummary:
    task_id: str
    title: str
    assignee: str
    status: str
    priority: int
    created_at: datetime
    updated_at: datetime
    session_id: str
    run_count: int
    parent_count: int
    child_count: int
    summary: str
    error: str


@dataclass(frozen=True)
class KanbanBoardResult:
    tasks: tuple[KanbanTaskSummary, ...]
    truncated: bool


@dataclass(frozen=True)
class KanbanRelatedTask:
    task_id: str
    title: str
    assignee: str
    status: str


@dataclass(frozen=True)
class KanbanRun:
    run_id: int
    profile: str
    step_key: str
    status: str
    started_at: datetime | None
    ended_at: datetime | None
    outcome: str
    summary: str
    metadata: str
    error: str


@dataclass(frozen=True)
class KanbanEvent:
    event_id: int
    run_id: int | None
    kind: str
    payload: str
    created_at: datetime


@dataclass(frozen=True)
class KanbanComment:
    comment_id: int
    author: str
    body: str
    created_at: datetime


@dataclass(frozen=True)
class KanbanAttachment:
    attachment_id: int
    filename: str
    content_type: str
    size: int
    uploaded_by: str
    created_at: datetime


@dataclass(frozen=True)
class KanbanDelivery:
    platform: str
    has_chat_id: bool
    has_thread_id: bool
    has_user_id: bool
    notifier_profile: str
    created_at: datetime
    last_event_id: int


@dataclass(frozen=True)
class KanbanTaskDetail:
    task: KanbanTaskSummary
    body: str
    created_by: str
    started_at: datetime | None
    completed_at: datetime | None
    workspace_kind: str
    project_id: str
    result: str
    block_kind: str
    current_step: str
    consecutive_failures: int
    goal_mode: bool
    parents: tuple[KanbanRelatedTask, ...]
    children: tuple[KanbanRelatedTask, ...]
    runs: tuple[KanbanRun, ...]
    events: tuple[KanbanEvent, ...]
    comments: tuple[KanbanComment, ...]
    attachments: tuple[KanbanAttachment, ...]
    deliveries: tuple[KanbanDelivery, ...]
    runs_truncated: bool = False


@dataclass(frozen=True)
class AgentCronJob:
    profile: str
    job_id: str
    name: str
    enabled: bool
    state: str
    cadence: str
    task: str
    script: str
    mode: str
    last_run_at: datetime | None
    next_run_at: datetime | None
    last_status: str
    last_error: str
    scheduler: str
    heartbeat_at: datetime | None
    schedule_kind: str = ""
    schedule_expression: str = ""
    interval_minutes: int = 0


@dataclass(frozen=True)
class AgentCronExecution:
    execution_id: str
    profile: str
    job_id: str
    source: str
    status: str
    claimed_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    error: str


@dataclass(frozen=True)
class CronSnapshot:
    jobs: tuple[AgentCronJob, ...]
    executions: tuple[AgentCronExecution, ...]
    jobs_truncated: bool
    executions_truncated: bool
    read_at: datetime


def _timestamp(value: object) -> datetime:
    try:
        return datetime.fromtimestamp(float(value), UTC)
    except (TypeError, ValueError, OSError):
        return datetime.fromtimestamp(0, UTC)


def _optional_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), UTC)
    except (TypeError, ValueError, OSError):
        return None


def _optional_iso_timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class AgentRuntimeProvider:
    """Read agent runtime state through a fixed, non-mutating in-pod query."""

    def __init__(
        self,
        target: DeploymentTarget,
        *,
        runner: KubeRunner | None = None,
    ) -> None:
        if not (
            is_valid_project_id(target.project_id)
            and is_valid_cluster_name(target.cluster_name)
            and is_valid_location(target.location)
            and is_valid_namespace(target.namespace)
        ):
            raise ValueError("invalid agent runtime target")
        self.target = target
        self.context = (
            f"gke_{target.project_id}_{target.location}_{target.cluster_name}"
        )
        self.runner = runner or KubectlRunner(target)

    def _base(self) -> list[str]:
        return ["--context", self.context, "-n", self.target.namespace]

    def _json(self, result: KubeCommandResult, component: str) -> dict:
        if result.returncode != 0:
            error = result.stderr.lower()
            guidance = kube_failure_guidance(result)
            if not guidance:
                if result.timed_out:
                    guidance = "Check cluster connectivity, then retry."
                elif "context" in error or "not found" in error:
                    guidance = (
                        "Confirm the selected project, cluster, and location, "
                        "then retry."
                    )
                elif "forbidden" in error or "permission" in error:
                    guidance = (
                        "Request read access to PlatformAgent resources and pods/exec "
                        "in the selected namespace."
                    )
                else:
                    guidance = (
                        "Confirm the cluster, namespace, gateway pod, and kubectl access."
                    )
            raise AgentRuntimeError(f"{component} failed.", guidance)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise AgentRuntimeError(
                f"{component} returned invalid data.",
                "Check the selected gateway pod and agent runtime installation.",
            ) from exc
        if not isinstance(payload, dict):
            raise AgentRuntimeError(f"{component} returned invalid data.")
        return payload

    def list_agents(self) -> tuple[str, ...]:
        """Discover deployed agents from running gateway pods.

        History lives in the gateway PVC, so pod discovery is the narrowest
        relevant source. Current deployments label credential-proxy pods; an
        older deployment may lack that label, so a bounded PlatformAgent list
        supplies validated app names for a second pod query.
        """
        result = self.runner.run(
            [
                *self._base(),
                "get",
                "pods",
                "-l",
                "kubeagents.x-k8s.io/has-credential-proxy=true",
                "--field-selector=status.phase=Running",
                "-o",
                "json",
            ],
            timeout=15,
        )
        payload = self._json(result, "Gateway discovery")
        agents = self._agent_names(payload)
        if agents:
            return agents

        resources = self.runner.run(
            [*self._base(), "get", "platformagents", "-o", "json"],
            timeout=15,
        )
        resource_payload = self._json(resources, "PlatformAgent discovery")
        candidates = sorted(
            {
                str((item.get("metadata") or {}).get("name") or "")
                for item in resource_payload.get("items", [])[:50]
            }
        )
        candidates = [name for name in candidates if _K8S_NAME.fullmatch(name)]
        if not candidates:
            return ()
        selector = "app in (" + ",".join(f"{name}-gateway" for name in candidates) + ")"
        fallback = self.runner.run(
            [
                *self._base(),
                "get",
                "pods",
                "-l",
                selector,
                "--field-selector=status.phase=Running",
                "-o",
                "json",
            ],
            timeout=15,
        )
        return self._agent_names(self._json(fallback, "Gateway discovery"))

    @staticmethod
    def _agent_names(payload: dict) -> tuple[str, ...]:
        agents = set()
        for item in payload.get("items", []):
            labels = (item.get("metadata") or {}).get("labels") or {}
            app = str(labels.get("app") or "")
            if app.endswith("-gateway"):
                name = app.removesuffix("-gateway")
                if _K8S_NAME.fullmatch(name):
                    agents.add(name)
        return tuple(sorted(agents))

    def _gateway_pod(self, agent: str) -> str:
        if not _K8S_NAME.fullmatch(agent):
            raise ValueError("invalid PlatformAgent name")
        result = self.runner.run(
            [
                *self._base(),
                "get",
                "pods",
                "-l",
                f"app={agent}-gateway",
                "--field-selector=status.phase=Running",
                "-o",
                "json",
            ],
            timeout=15,
        )
        payload = self._json(result, "Gateway discovery")
        pods = sorted(
            str((item.get("metadata") or {}).get("name") or "")
            for item in payload.get("items", [])
            if (item.get("metadata") or {}).get("name")
        )
        if not pods:
            raise AgentRuntimeError(
                "No running gateway pod was found.",
                f"Check PlatformAgent {agent} in namespace {self.target.namespace}.",
            )
        return pods[0]

    def _read(self, agent: str, arguments: list[str], *, timeout: int = 25) -> dict:
        pod = self._gateway_pod(agent)
        result = self.runner.run(
            [
                *self._base(),
                "exec",
                pod,
                "-c",
                "platform-agent",
                "--",
                "/opt/hermes/.venv/bin/python3",
                "-c",
                _READ_SCRIPT,
                *arguments,
            ],
            timeout=timeout,
        )
        return self._json(result, "Agent runtime read")

    def check_connection(self, agent: str) -> tuple[int, int]:
        payload = self._read(agent, ["health"])
        profiles = payload.get("profiles", [])
        return len(profiles), sum(int(item.get("sessions") or 0) for item in profiles)

    def list_conversations(
        self,
        agent: str,
        *,
        cutoff: datetime,
        limit: int = 200,
    ) -> HistoryResult:
        limit = max(1, min(limit, 500))
        payload = self._read(
            agent,
            ["conversations", str(cutoff.timestamp()), str(limit)],
        )
        conversations = []
        for row in payload.get("conversations", []):
            conversations.append(
                AgentConversation(
                    session_id=str(row.get("id") or ""),
                    profile=str(row.get("profile") or "default"),
                    platform=str(row.get("platform") or "unknown"),
                    user=str(row.get("user") or ""),
                    attribution=str(row.get("attribution") or "unattributed"),
                    title=str(row.get("title") or ""),
                    preview=redact_evidence(row.get("preview") or ""),
                    chat_type=str(row.get("chat_type") or ""),
                    chat_id=str(row.get("chat_id") or ""),
                    thread_id=str(row.get("thread_id") or ""),
                    started_at=_timestamp(row.get("started_at")),
                    last_active=_timestamp(row.get("last_active")),
                    message_count=int(row.get("chat_message_count") or 0),
                    tool_call_count=int(row.get("tool_call_count") or 0),
                )
            )
        return HistoryResult(tuple(conversations), bool(payload.get("truncated")))

    def get_messages(
        self,
        agent: str,
        *,
        profile: str,
        session_id: str,
        limit: int = 500,
    ) -> MessageResult:
        if not profile or len(profile) > 128 or not session_id or len(session_id) > 256:
            raise ValueError("invalid session selection")
        limit = max(1, min(limit, 500))
        payload = self._read(
            agent,
            ["messages", profile, session_id, str(limit)],
        )
        messages = tuple(
            AgentMessage(
                message_id=int(row.get("id") or 0),
                role=str(row.get("role") or "assistant"),
                content=redact_evidence(row.get("content") or ""),
                occurred_at=_timestamp(row.get("timestamp")),
            )
            for row in payload.get("messages", [])
        )
        return MessageResult(messages, bool(payload.get("truncated")))

    def get_task_updates(
        self,
        agent: str,
        *,
        session_id: str,
        limit: int = 100,
    ) -> TaskUpdateResult:
        """Read specialist work linked to one agent session."""
        if not session_id or len(session_id) > 256:
            raise ValueError("invalid session selection")
        limit = max(1, min(limit, 200))
        payload = self._read(agent, ["tasks", session_id, str(limit)])
        tasks = tuple(
            AgentTaskUpdate(
                task_id=str(row.get("id") or ""),
                title=redact_evidence(row.get("title") or ""),
                assignee=str(row.get("assignee") or "unassigned"),
                status=str(row.get("status") or "unknown"),
                created_at=_timestamp(row.get("created_at")),
                updated_at=_timestamp(row.get("updated_at")),
                summary=redact_evidence(row.get("summary") or ""),
                error=redact_evidence(row.get("error") or ""),
                run_count=int(row.get("run_count") or 0),
                latest_event=str(row.get("latest_event") or ""),
                latest_event_at=_optional_timestamp(row.get("latest_event_at")),
                previous_error=redact_evidence(row.get("previous_error") or ""),
            )
            for row in payload.get("tasks", [])
        )
        return TaskUpdateResult(tasks, bool(payload.get("truncated")))

    def list_kanban_tasks(
        self,
        agent: str,
        *,
        limit: int = 500,
    ) -> KanbanBoardResult:
        """Read the bounded cross-session Kanban board."""
        limit = max(1, min(limit, 500))
        payload = self._read(agent, ["board", str(limit)])
        tasks = tuple(
            KanbanTaskSummary(
                task_id=str(row.get("id") or ""),
                title=redact_evidence(row.get("title") or ""),
                assignee=str(row.get("assignee") or "unassigned"),
                status=str(row.get("status") or "unknown"),
                priority=int(row.get("priority") or 0),
                created_at=_timestamp(row.get("created_at")),
                updated_at=_timestamp(row.get("updated_at")),
                session_id=str(row.get("session_id") or ""),
                run_count=int(row.get("run_count") or 0),
                parent_count=int(row.get("parent_count") or 0),
                child_count=int(row.get("child_count") or 0),
                summary=redact_evidence(row.get("summary") or ""),
                error=redact_evidence(row.get("error") or ""),
            )
            for row in payload.get("tasks", [])
        )
        return KanbanBoardResult(tasks, bool(payload.get("truncated")))

    def get_cron_snapshot(
        self,
        agent: str,
        *,
        job_limit: int = 500,
        execution_limit: int = 500,
    ) -> CronSnapshot:
        """Read configured jobs, scheduler health, and bounded executions."""
        job_limit = max(1, min(job_limit, 500))
        execution_limit = max(1, min(execution_limit, 500))
        payload = self._read(
            agent,
            ["cron", str(job_limit), str(execution_limit)],
        )
        jobs = tuple(
            AgentCronJob(
                profile=str(row.get("profile") or "default"),
                job_id=str(row.get("id") or ""),
                name=redact_evidence(row.get("name") or "Unnamed job"),
                enabled=bool(row.get("enabled")),
                state=str(row.get("state") or "unknown"),
                cadence=str(row.get("cadence") or "—"),
                task=redact_evidence(row.get("prompt") or ""),
                script=str(row.get("script") or ""),
                mode=str(row.get("mode") or "agent"),
                last_run_at=_optional_iso_timestamp(row.get("last_run_at")),
                next_run_at=_optional_iso_timestamp(row.get("next_run_at")),
                last_status=str(row.get("last_status") or "never"),
                last_error=redact_evidence(row.get("last_error") or ""),
                scheduler=str(row.get("scheduler") or "missing"),
                heartbeat_at=_optional_timestamp(row.get("heartbeat_at")),
                schedule_kind=str(row.get("schedule_kind") or ""),
                schedule_expression=str(row.get("schedule_expression") or ""),
                interval_minutes=int(row.get("interval_minutes") or 0),
            )
            for row in payload.get("jobs", [])
        )
        executions = tuple(
            AgentCronExecution(
                execution_id=str(row.get("id") or ""),
                profile=str(row.get("profile") or "default"),
                job_id=str(row.get("job_id") or ""),
                source=str(row.get("source") or "unknown"),
                status=str(row.get("status") or "unknown"),
                claimed_at=_optional_iso_timestamp(row.get("claimed_at")),
                started_at=_optional_iso_timestamp(row.get("started_at")),
                finished_at=_optional_iso_timestamp(row.get("finished_at")),
                error=redact_evidence(row.get("error") or ""),
            )
            for row in payload.get("executions", [])
        )
        return CronSnapshot(
            jobs,
            executions,
            bool(payload.get("jobs_truncated")),
            bool(payload.get("executions_truncated")),
            _timestamp(payload.get("read_at")),
        )

    def get_kanban_task(self, agent: str, task_id: str) -> KanbanTaskDetail:
        """Read one task's useful execution, relationship, and delivery state."""
        if not _KANBAN_TASK.fullmatch(task_id):
            raise ValueError("invalid Task Kanban task")
        payload = self._read(agent, ["task", task_id])
        row = payload.get("task") or {}
        if not row:
            raise AgentRuntimeError("Task Kanban task was not found.")

        def related(item: dict) -> KanbanRelatedTask:
            return KanbanRelatedTask(
                task_id=str(item.get("id") or ""),
                title=redact_evidence(item.get("title") or ""),
                assignee=str(item.get("assignee") or "unassigned"),
                status=str(item.get("status") or "unknown"),
            )

        runs = tuple(
            KanbanRun(
                run_id=int(item.get("id") or 0),
                profile=str(item.get("profile") or ""),
                step_key=str(item.get("step_key") or ""),
                status=str(item.get("status") or "unknown"),
                started_at=_optional_timestamp(item.get("started_at")),
                ended_at=_optional_timestamp(item.get("ended_at")),
                outcome=str(item.get("outcome") or ""),
                summary=redact_evidence(item.get("summary") or ""),
                metadata=redact_evidence(item.get("metadata") or ""),
                error=redact_evidence(item.get("error") or ""),
            )
            for item in payload.get("runs", [])
        )
        parents = tuple(related(item) for item in payload.get("parents", []))
        children = tuple(related(item) for item in payload.get("children", []))
        latest = runs[-1] if runs else None
        summary = latest.summary if latest else ""
        error = latest.error if latest else str(row.get("last_failure_error") or "")
        task = KanbanTaskSummary(
            task_id=str(row.get("id") or task_id),
            title=redact_evidence(row.get("title") or ""),
            assignee=str(row.get("assignee") or "unassigned"),
            status=str(row.get("status") or "unknown"),
            priority=int(row.get("priority") or 0),
            created_at=_timestamp(row.get("created_at")),
            updated_at=_timestamp(row.get("updated_at")),
            session_id=str(row.get("session_id") or ""),
            run_count=int(row.get("total_run_count") or row.get("run_count") or 0),
            parent_count=len(parents),
            child_count=len(children),
            summary=summary,
            error=redact_evidence(error),
        )
        return KanbanTaskDetail(
            task=task,
            body=redact_evidence(row.get("body") or ""),
            created_by=str(row.get("created_by") or ""),
            started_at=_optional_timestamp(row.get("started_at")),
            completed_at=_optional_timestamp(row.get("completed_at")),
            workspace_kind=str(row.get("workspace_kind") or ""),
            project_id=str(row.get("project_id") or ""),
            result=redact_evidence(row.get("result") or ""),
            block_kind=str(row.get("block_kind") or ""),
            current_step=str(row.get("current_step_key") or ""),
            consecutive_failures=int(row.get("consecutive_failures") or 0),
            goal_mode=bool(row.get("goal_mode")),
            parents=parents,
            children=children,
            runs=runs,
            events=tuple(
                KanbanEvent(
                    event_id=int(item.get("id") or 0),
                    run_id=(
                        int(item["run_id"])
                        if item.get("run_id") is not None
                        else None
                    ),
                    kind=str(item.get("kind") or "unknown"),
                    payload=redact_evidence(item.get("payload") or ""),
                    created_at=_timestamp(item.get("created_at")),
                )
                for item in payload.get("events", [])
            ),
            comments=tuple(
                KanbanComment(
                    comment_id=int(item.get("id") or 0),
                    author=str(item.get("author") or ""),
                    body=redact_evidence(item.get("body") or ""),
                    created_at=_timestamp(item.get("created_at")),
                )
                for item in payload.get("comments", [])
            ),
            attachments=tuple(
                KanbanAttachment(
                    attachment_id=int(item.get("id") or 0),
                    filename=redact_evidence(item.get("filename") or ""),
                    content_type=str(item.get("content_type") or ""),
                    size=int(item.get("size") or 0),
                    uploaded_by=str(item.get("uploaded_by") or ""),
                    created_at=_timestamp(item.get("created_at")),
                )
                for item in payload.get("attachments", [])
            ),
            deliveries=tuple(
                KanbanDelivery(
                    platform=str(item.get("platform") or "unknown"),
                    has_chat_id=bool(item.get("has_chat_id")),
                    has_thread_id=bool(item.get("has_thread_id")),
                    has_user_id=bool(item.get("has_user_id")),
                    notifier_profile=str(item.get("notifier_profile") or ""),
                    created_at=_timestamp(item.get("created_at")),
                    last_event_id=int(item.get("last_event_id") or 0),
                )
                for item in payload.get("deliveries", [])
            ),
            runs_truncated=bool(payload.get("runs_truncated")),
        )
