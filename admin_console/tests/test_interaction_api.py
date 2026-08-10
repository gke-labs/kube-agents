from __future__ import annotations

import time
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock

from fastapi.testclient import TestClient

from admin_console.agent_chat import ChatRunResult
from admin_console.agent_runtime import AgentTaskUpdate, TaskUpdateResult
from admin_console.api.app import create_app
from admin_console.chat.service import ChatService
from admin_console.chat.models import Interaction, InteractionStatus
from admin_console.chat.store import SQLiteInteractionStore


def task(task_id: str, status: str, *, error: str = "") -> AgentTaskUpdate:
    now = datetime.now(UTC)
    return AgentTaskUpdate(
        task_id=task_id,
        title="Check cluster capacity",
        assignee="cluster-agent",
        status=status,
        created_at=now,
        updated_at=now,
        summary="Capacity checked" if status == "done" else "",
        error=error,
    )


class ScriptedBackend:
    def __init__(
        self,
        *,
        root: ChatRunResult | None = None,
        task_snapshots: list[TaskUpdateResult] | None = None,
    ) -> None:
        self.root = root or ChatRunResult(
            run_id="run_0123456789abcdef0123456789abcdef",
            session_id="portal_0123456789abcdef0123456789abcdef",
            status="completed",
            output="The cluster is healthy.",
        )
        self.task_snapshots = task_snapshots or [TaskUpdateResult((), False)]
        self._lock = Lock()
        self.task_reads = 0
        self.approvals: list[str] = []

    def run(self, *args, **kwargs) -> ChatRunResult:
        return self.root

    def resolve_approval(self, *args, **kwargs) -> ChatRunResult:
        self.approvals.append(kwargs["choice"])
        return ChatRunResult(
            run_id=kwargs["run_id"],
            session_id="portal_0123456789abcdef0123456789abcdef",
            status="completed",
            output="Approved operation completed.",
        )

    def stop(self, *args, **kwargs) -> None:
        return None

    def get_task_updates(self, *args, **kwargs) -> TaskUpdateResult:
        with self._lock:
            index = min(self.task_reads, len(self.task_snapshots) - 1)
            self.task_reads += 1
            return self.task_snapshots[index]


def client_for(backend: ScriptedBackend) -> tuple[TestClient, ChatService]:
    service = ChatService(
        lambda: backend,
        poll_interval=0.001,
        quiet_polls=2,
        task_timeout=1,
    )
    return TestClient(create_app(service)), service


class InteractionApiTest(unittest.TestCase):
    def start(self, client: TestClient) -> str:
        response = client.post(
            "/api/v1/interactions",
            json={
                "agentId": "platform-agent",
                "input": {"text": "Is the cluster healthy?"},
            },
        )
        self.assertEqual(response.status_code, 202)
        return response.json()["interactionId"]

    def wait_for_terminal(
        self,
        client: TestClient,
        interaction_id: str,
    ) -> dict:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            payload = client.get(
                f"/api/v1/interactions/{interaction_id}"
            ).json()
            if payload["terminal"]:
                return payload
            time.sleep(0.005)
        self.fail("interaction did not become terminal")

    def test_completed_root_is_not_terminal_until_delegated_work_settles(self):
        backend = ScriptedBackend(
            task_snapshots=[
                TaskUpdateResult((task("task-1", "running"),), False),
                TaskUpdateResult((task("task-1", "done"),), False),
            ]
        )
        client, _ = client_for(backend)
        interaction_id = self.start(client)

        result = self.wait_for_terminal(client, interaction_id)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["output"], "The cluster is healthy.")
        self.assertEqual(result["tasks"][0]["status"], "done")
        self.assertGreaterEqual(backend.task_reads, 3)

    def test_failed_delegated_work_returns_diagnostics(self):
        backend = ScriptedBackend(
            task_snapshots=[
                TaskUpdateResult(
                    (task("task-broken", "failed", error="quota exhausted"),),
                    False,
                )
            ]
        )
        client, _ = client_for(backend)
        interaction_id = self.start(client)

        result = self.wait_for_terminal(client, interaction_id)

        self.assertEqual(result["status"], "failed")
        self.assertIn("quota exhausted", result["error"])
        self.assertIn("Task Kanban", result["diagnostics"][0])

    def test_approval_resumes_the_same_interaction(self):
        backend = ScriptedBackend(
            root=ChatRunResult(
                run_id="run_0123456789abcdef0123456789abcdef",
                session_id="portal_0123456789abcdef0123456789abcdef",
                status="waiting_for_approval",
                approval={"tool": "kubectl", "reason": "Needs confirmation"},
            )
        )
        client, _ = client_for(backend)
        interaction_id = self.start(client)
        deadline = time.monotonic() + 2
        waiting = {}
        while time.monotonic() < deadline:
            waiting = client.get(
                f"/api/v1/interactions/{interaction_id}"
            ).json()
            if waiting["status"] == "waiting_for_approval":
                break
            time.sleep(0.005)
        self.assertEqual(waiting["approval"]["tool"], "kubectl")

        response = client.post(
            f"/api/v1/interactions/{interaction_id}/approval",
            json={"choice": "once"},
        )
        self.assertEqual(response.status_code, 200)
        result = self.wait_for_terminal(client, interaction_id)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(backend.approvals, ["once"])

    def test_event_stream_has_ordered_aggregate_lifecycle(self):
        client, _ = client_for(ScriptedBackend())
        interaction_id = self.start(client)
        self.wait_for_terminal(client, interaction_id)

        response = client.get(
            f"/api/v1/interactions/{interaction_id}/events?after=0&waitSeconds=0"
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("event: root.completed", response.text)
        self.assertIn("event: interaction.completed", response.text)
        ids = [
            int(line.removeprefix("id: "))
            for line in response.text.splitlines()
            if line.startswith("id: ")
        ]
        self.assertEqual(ids, list(range(1, len(ids) + 1)))

    def test_command_rejects_unknown_fields(self):
        client, _ = client_for(ScriptedBackend())

        response = client.post(
            "/api/v1/interactions",
            json={
                "agentId": "platform-agent",
                "input": {"text": "hello"},
                "targetOverride": "untrusted",
            },
        )

        self.assertEqual(response.status_code, 422)

    def test_sqlite_store_preserves_terminal_interaction_and_events(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "interactions.db"
            service = ChatService(
                lambda: ScriptedBackend(),
                store=SQLiteInteractionStore(path),
                poll_interval=0.001,
                quiet_polls=2,
                task_timeout=1,
            )
            interaction = service.start(
                agent_id="platform-agent",
                input_text="Is the cluster healthy?",
            )
            completed = service.wait(interaction.interaction_id, timeout=2)
            self.assertEqual(completed.status, InteractionStatus.COMPLETED)

            reopened = SQLiteInteractionStore(path)
            persisted = reopened.get(interaction.interaction_id)
            events = reopened.events_after(interaction.interaction_id)

            self.assertEqual(persisted.output, "The cluster is healthy.")
            self.assertEqual(events[-1].event, "interaction.completed")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_restart_fails_incomplete_interaction_instead_of_inferring_success(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteInteractionStore(Path(directory) / "interactions.db")
            now = datetime.now(UTC)
            interaction = Interaction(
                interaction_id="ix_incomplete",
                agent_id="platform-agent",
                profile="default",
                session_id="portal_incomplete",
                input_text="Perform asynchronous work",
                status=InteractionStatus.WAITING_FOR_TASKS,
                created_at=now,
                updated_at=now,
                root_run_id="run_0123456789abcdef0123456789abcdef",
                output="I delegated the work.",
            )
            store.create(interaction)
            store.append_event(interaction.interaction_id, "root.completed")

            service = ChatService(lambda: ScriptedBackend(), store=store)
            recovered = service.get(interaction.interaction_id)

            self.assertEqual(service.recovered_interactions, 1)
            self.assertEqual(recovered.status, InteractionStatus.FAILED)
            self.assertIn("restarted", recovered.error)
            self.assertIn("will not infer success", recovered.diagnostics[0])
            self.assertEqual(
                store.events_after(interaction.interaction_id)[-1].event,
                "interaction.recovery_failed",
            )

    def test_durable_store_prunes_expired_terminal_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteInteractionStore(Path(directory) / "interactions.db")
            old = datetime.now(UTC) - timedelta(days=8)
            interaction = Interaction(
                interaction_id="ix_expired",
                agent_id="platform-agent",
                profile="default",
                session_id="portal_expired",
                input_text="Old request",
                status=InteractionStatus.COMPLETED,
                created_at=old,
                updated_at=old,
                output="Old response",
            )
            store.create(interaction)

            service = ChatService(lambda: ScriptedBackend(), store=store)

            self.assertEqual(service.pruned_interactions, 1)
            self.assertIsNone(store.get(interaction.interaction_id))


if __name__ == "__main__":
    unittest.main()
