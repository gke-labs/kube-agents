from __future__ import annotations

import os
import time
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Lock
from unittest.mock import patch

from fastapi.testclient import TestClient

from admin_console.agent_chat import ChatRunResult, MAX_HISTORY_MESSAGES
from admin_console.agent_runtime import AgentTaskUpdate, TaskUpdateResult
from admin_console.api.app import create_app
from admin_console.chat.service import ChatService
from admin_console.chat.models import Interaction, InteractionStatus
from admin_console.chat.store import SQLiteInteractionStore
from admin_console.clients.portal_api import PortalApiClient, PortalApiError
from admin_console.connection_persistence import save_connection
from admin_console.project_config import (
    DeploymentTarget,
    deployment_target_headers,
)


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
            events=(
                {"event": "tool.started", "tool": "kanban_create"},
                {
                    "event": "tool.completed",
                    "tool": "kanban_create",
                    "error": False,
                },
            ),
        )
        self.task_snapshots = task_snapshots or [TaskUpdateResult((), False)]
        self._lock = Lock()
        self.task_reads = 0
        self.prompts: list[str] = []
        self.approvals: list[str] = []
        self.approval_resolved = Event()

    def run(self, *args, **kwargs) -> ChatRunResult:
        self.prompts.append(kwargs["prompt"])
        if self.root.status == "waiting_for_approval":
            kwargs["on_update"](self.root)
            self.approval_resolved.wait(timeout=2)
            return ChatRunResult(
                run_id=self.root.run_id,
                session_id=self.root.session_id,
                status="completed",
                output="Approved operation completed.",
            )
        return self.root

    def resolve_approval(self, *args, **kwargs) -> ChatRunResult:
        self.approvals.append(kwargs["choice"])
        self.approval_resolved.set()
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


class BlockingBackend(ScriptedBackend):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()
        self.run_calls = 0

    def run(self, *args, **kwargs) -> ChatRunResult:
        self.run_calls += 1
        self.started.set()
        self.release.wait(timeout=2)
        return self.root


class CancellableBackend(ScriptedBackend):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.stopped = Event()
        self.stop_calls = 0

    def run(self, *args, **kwargs) -> ChatRunResult:
        kwargs["on_update"](
            ChatRunResult(
                run_id=self.root.run_id,
                session_id=self.root.session_id,
                status="running",
            )
        )
        self.started.set()
        self.stopped.wait(timeout=2)
        return ChatRunResult(
            run_id=self.root.run_id,
            session_id=self.root.session_id,
            status="cancelled",
        )

    def stop(self, *args, **kwargs) -> None:
        self.stop_calls += 1
        self.stopped.set()


class FlakyTaskBackend(ScriptedBackend):
    def __init__(self, failures: int) -> None:
        super().__init__(
            task_snapshots=[TaskUpdateResult((task("task-1", "done"),), False)]
        )
        self.failures = failures

    def get_task_updates(self, *args, **kwargs) -> TaskUpdateResult:
        if self.task_reads < self.failures:
            self.task_reads += 1
            raise RuntimeError("transient task read")
        return super().get_task_updates(*args, **kwargs)


def client_for(
    backend: ScriptedBackend,
    *,
    max_workers: int = 4,
) -> tuple[TestClient, ChatService]:
    service = ChatService(
        lambda: backend,
        poll_interval=0.001,
        quiet_polls=2,
        task_timeout=1,
        max_workers=max_workers,
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
        self.assertEqual(
            result["toolCalls"],
            [
                {
                    "name": "kanban_create",
                    "status": "completed",
                    "source": "root_run",
                }
            ],
        )
        self.assertGreaterEqual(backend.task_reads, 3)

    def test_cancelled_queued_interaction_is_never_started(self):
        backend = BlockingBackend()
        service = ChatService(
            lambda: backend,
            poll_interval=0.001,
            quiet_polls=2,
            task_timeout=1,
            max_workers=1,
        )
        first = service.start(agent_id="platform-agent", input_text="first")
        self.assertTrue(backend.started.wait(timeout=1))
        second = service.start(agent_id="platform-agent", input_text="second")

        cancelled = service.cancel(second.interaction_id)
        backend.release.set()
        service.wait(first.interaction_id, timeout=2)
        time.sleep(0.05)

        self.assertEqual(cancelled.status, InteractionStatus.CANCELLED)
        self.assertEqual(service.get(second.interaction_id).status, InteractionStatus.CANCELLED)
        self.assertEqual(backend.run_calls, 1)
        events = service.store.events_after(second.interaction_id)
        self.assertNotIn("interaction.started", [event.event for event in events])

    def test_cancel_running_interaction_stops_known_root_run(self):
        backend = CancellableBackend()
        service = ChatService(lambda: backend, poll_interval=0.001, task_timeout=1)
        interaction = service.start(agent_id="platform-agent", input_text="long run")
        self.assertTrue(backend.started.wait(timeout=1))

        cancelled = service.cancel(interaction.interaction_id)
        final = service.wait(interaction.interaction_id, timeout=2)

        self.assertEqual(cancelled.status, InteractionStatus.CANCELLED)
        self.assertEqual(cancelled.root_run_id, backend.root.run_id)
        self.assertEqual(final.status, InteractionStatus.CANCELLED)
        self.assertEqual(backend.stop_calls, 1)
        self.assertTrue(backend.stopped.is_set())

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

    def test_blocked_and_crashed_delegated_work_are_failures(self):
        for status in ("blocked", "crashed"):
            with self.subTest(status=status):
                backend = ScriptedBackend(
                    task_snapshots=[TaskUpdateResult((task("task-1", status),), False)]
                )
                client, _ = client_for(backend)
                interaction_id = self.start(client)

                result = self.wait_for_terminal(client, interaction_id)

                self.assertEqual(result["status"], "failed")
                self.assertIn(status, result["error"])

    def test_all_in_flight_task_states_wait_for_completion(self):
        for status in ("triage", "todo", "ready", "scheduled", "running", "review"):
            with self.subTest(status=status):
                backend = ScriptedBackend(
                    task_snapshots=[
                        TaskUpdateResult((task("task-1", status),), False),
                        TaskUpdateResult((task("task-1", "done"),), False),
                    ]
                )
                client, _ = client_for(backend)
                interaction_id = self.start(client)

                result = self.wait_for_terminal(client, interaction_id)

                self.assertEqual(result["status"], "completed")
                self.assertGreaterEqual(backend.task_reads, 3)

    def test_truncated_task_snapshot_cannot_settle_interaction(self):
        backend = ScriptedBackend(
            task_snapshots=[
                TaskUpdateResult((task("task-1", "done"),), True),
                TaskUpdateResult((task("task-1", "running"),), False),
                TaskUpdateResult((task("task-1", "done"),), False),
            ]
        )
        client, _ = client_for(backend)
        interaction_id = self.start(client)

        result = self.wait_for_terminal(client, interaction_id)

        self.assertEqual(result["status"], "completed")
        self.assertGreaterEqual(backend.task_reads, 4)

    def test_archived_and_unknown_task_states_do_not_pin_settlement(self):
        for status in ("archived", "future-terminal-state", ""):
            with self.subTest(status=status):
                backend = ScriptedBackend(
                    task_snapshots=[TaskUpdateResult((task("task-1", status),), False)]
                )
                client, _ = client_for(backend)
                interaction_id = self.start(client)

                result = self.wait_for_terminal(client, interaction_id)

                self.assertEqual(result["status"], "completed")

    def test_one_transient_task_read_is_retried(self):
        backend = FlakyTaskBackend(1)
        client, _ = client_for(backend)
        interaction_id = self.start(client)

        result = self.wait_for_terminal(client, interaction_id)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["output"], "The cluster is healthy.")
        self.assertGreaterEqual(backend.task_reads, 3)

    def test_repeated_task_read_failures_still_fail_boundedly(self):
        backend = FlakyTaskBackend(3)
        client, _ = client_for(backend)
        interaction_id = self.start(client)

        result = self.wait_for_terminal(client, interaction_id)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["output"], "The cluster is healthy.")
        self.assertEqual(backend.task_reads, 3)

    def test_approval_resumes_the_same_interaction(self):
        backend = ScriptedBackend(
            root=ChatRunResult(
                run_id="run_0123456789abcdef0123456789abcdef",
                session_id="portal_0123456789abcdef0123456789abcdef",
                status="waiting_for_approval",
                approval={"tool": "kubectl", "reason": "Needs confirmation"},
            )
        )
        client, _ = client_for(backend, max_workers=1)
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

    def test_approval_keeps_stream_evidence_until_tool_completion(self):
        backend = ScriptedBackend(
            root=ChatRunResult(
                run_id="run_0123456789abcdef0123456789abcdef",
                session_id="portal_0123456789abcdef0123456789abcdef",
                status="waiting_for_approval",
                approval={"tool": "kubectl", "reason": "Needs confirmation"},
                events=({"event": "tool.started", "tool": "kubectl"},),
            )
        )

        original_run = backend.run

        def run_with_completion(*args, **kwargs):
            original_run(*args, **kwargs)
            return ChatRunResult(
                run_id=backend.root.run_id,
                session_id=backend.root.session_id,
                status="completed",
                output="Approved operation completed.",
                events=({"event": "tool.completed", "tool": "kubectl"},),
            )

        backend.run = run_with_completion
        client, _ = client_for(backend)
        interaction_id = self.start(client)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            waiting = client.get(f"/api/v1/interactions/{interaction_id}").json()
            if waiting["status"] == "waiting_for_approval":
                break
            time.sleep(0.005)

        response = client.post(
            f"/api/v1/interactions/{interaction_id}/approval",
            json={"choice": "once"},
        )
        self.assertEqual(response.status_code, 200)
        result = self.wait_for_terminal(client, interaction_id)

        self.assertEqual(
            result["toolCalls"],
            [{"name": "kubectl", "status": "completed", "source": "root_run"}],
        )

    def test_only_one_concurrent_approval_is_accepted(self):
        backend = ScriptedBackend(
            root=ChatRunResult(
                run_id="run_0123456789abcdef0123456789abcdef",
                session_id="portal_0123456789abcdef0123456789abcdef",
                status="waiting_for_approval",
                approval={"tool": "kubectl"},
            )
        )
        client, service = client_for(backend)
        interaction_id = self.start(client)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if service.get(interaction_id).status == InteractionStatus.WAITING_FOR_APPROVAL:
                break
            time.sleep(0.005)

        def approve_once() -> bool:
            try:
                service.approve(interaction_id, "once")
                return True
            except ValueError:
                return False

        with ThreadPoolExecutor(max_workers=16) as executor:
            accepted = list(executor.map(lambda _: approve_once(), range(16)))
        result = service.wait(interaction_id, timeout=2)

        self.assertEqual(sum(accepted), 1)
        self.assertEqual(backend.approvals, ["once"])
        self.assertEqual(result.status, InteractionStatus.COMPLETED)
        events = service.store.events_after(interaction_id)
        self.assertEqual(
            sum(event.event == "approval.resolved" for event in events),
            1,
        )

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

    def test_command_rejects_non_portal_session_ids(self):
        client, service = client_for(ScriptedBackend())

        response = client.post(
            "/api/v1/interactions",
            json={
                "agentId": "platform-agent",
                "sessionId": "gchat-11ab",
                "input": {"text": "inject a turn"},
            },
        )

        self.assertEqual(response.status_code, 422)
        with self.assertRaisesRegex(ValueError, "portal-owned"):
            service.start(
                agent_id="platform-agent",
                session_id="slack-thread",
                input_text="inject a turn",
            )

    def test_client_sends_only_the_newest_supported_history(self):
        class AcceptedResponse:
            status_code = 202

            def json(self):
                return {
                    "interactionId": "ix_history_bound",
                    "sessionId": "portal_history_bound",
                    "status": "queued",
                    "terminal": False,
                }

        class RecordingTransport:
            request = {}

            def post(self, url: str, **kwargs):
                self.request = {"url": url, **kwargs}
                return AcceptedResponse()

        transport = RecordingTransport()
        client = PortalApiClient(transport=transport)
        history = [
            {"role": "user", "content": f"message-{index}"}
            for index in range(MAX_HISTORY_MESSAGES + 25)
        ]

        client.start_interaction(
            "platform-agent",
            prompt="continue",
            session_id="portal_history_bound",
            history=history,
        )

        sent = transport.request["json"]["history"]
        self.assertEqual(len(sent), MAX_HISTORY_MESSAGES)
        self.assertEqual(sent[0]["content"], "message-25")
        self.assertEqual(sent[-1]["content"], "message-124")

    def test_client_explains_api_validation_errors(self):
        client, _ = client_for(ScriptedBackend())
        response = client.post(
            "/api/v1/interactions",
            json={
                "agentId": "platform-agent",
                "input": {"text": "continue"},
                "history": [
                    {"role": "user", "content": "message"}
                    for _ in range(MAX_HISTORY_MESSAGES + 1)
                ],
            },
        )
        self.assertEqual(response.status_code, 422)

        with self.assertRaises(PortalApiError) as raised:
            PortalApiClient._payload(response)

        self.assertIn("history", str(raised.exception))
        self.assertIn(str(MAX_HISTORY_MESSAGES), str(raised.exception))

    def test_http_client_pins_every_request_to_its_selected_target(self):
        target = DeploymentTarget(
            "test-project-01",
            "test-cluster-01",
            "us-central1",
        )
        with patch("admin_console.clients.portal_api.httpx.Client") as constructor:
            client = PortalApiClient(target, base_url="http://127.0.0.1:8501/api/v1")

        self.assertEqual(client._target, target)
        self.assertEqual(
            constructor.call_args.kwargs["headers"],
            deployment_target_headers(target),
        )

    def test_api_rejects_a_stale_tab_target(self):
        current = DeploymentTarget(
            "test-project-01",
            "test-cluster-01",
            "us-central1",
        )
        stale = DeploymentTarget(
            "other-project-01",
            "other-cluster-01",
            "us-east1",
        )
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "KUBE_AGENTS_ADMIN_USER": "admin@example.com",
                "KUBE_AGENTS_ADMIN_CONNECTION_STATE": str(
                    Path(directory) / "connection.json"
                ),
            },
        ):
            save_connection("admin@example.com", current, datetime.now(UTC))
            client, _ = client_for(ScriptedBackend())

            response = client.get(
                "/api/v1/agents",
                headers=deployment_target_headers(stale),
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["error"]["code"], "stale_target_scope")

    def test_client_calls_the_cancel_endpoint(self):
        class CancelledResponse:
            status_code = 200

            def json(self):
                return {
                    "interactionId": "ix_cancel",
                    "sessionId": "portal_cancel",
                    "status": "cancelled",
                    "terminal": True,
                }

        class RecordingTransport:
            url = ""

            def post(self, url: str, **kwargs):
                self.url = url
                return CancelledResponse()

        transport = RecordingTransport()
        client = PortalApiClient(transport=transport)

        result = client.cancel_interaction("ix_cancel")

        self.assertEqual(transport.url, "interactions/ix_cancel/cancel")
        self.assertEqual(result.status, "cancelled")

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
            self.assertEqual(persisted.tool_calls[0].name, "kanban_create")
            self.assertEqual(events[-1].event, "interaction.completed")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_sqlite_store_redacts_prompts_but_backend_receives_the_original(self):
        backend = ScriptedBackend()
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteInteractionStore(Path(directory) / "interactions.db")
            service = ChatService(
                lambda: backend,
                store=store,
                poll_interval=0.001,
                quiet_polls=2,
                task_timeout=1,
            )
            secret_prompt = "Use api_key=AIza-test and password=hunter2"

            interaction = service.start(
                agent_id="platform-agent",
                input_text=secret_prompt,
            )
            completed = service.wait(interaction.interaction_id, timeout=2)
            persisted = store.get(interaction.interaction_id)

        self.assertEqual(backend.prompts, [secret_prompt])
        self.assertNotIn("AIza-test", persisted.input_text)
        self.assertNotIn("hunter2", persisted.input_text)
        self.assertNotIn("AIza-test", completed.to_dict()["input"]["text"])

    def test_sqlite_transition_allows_only_one_concurrent_winner(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteInteractionStore(Path(directory) / "interactions.db")
            now = datetime.now(UTC)
            interaction = Interaction(
                interaction_id="ix_approval_race",
                agent_id="platform-agent",
                profile="default",
                session_id="portal_approval_race",
                input_text="Approve this operation",
                status=InteractionStatus.WAITING_FOR_APPROVAL,
                created_at=now,
                updated_at=now,
            )
            store.create(interaction)

            def transition_once() -> bool:
                return (
                    store.transition(
                        interaction.interaction_id,
                        frozenset({InteractionStatus.WAITING_FOR_APPROVAL}),
                        status=InteractionStatus.RUNNING,
                    )
                    is not None
                )

            with ThreadPoolExecutor(max_workers=16) as executor:
                winners = list(executor.map(lambda _: transition_once(), range(16)))

            self.assertEqual(sum(winners), 1)
            self.assertEqual(
                store.get(interaction.interaction_id).status,
                InteractionStatus.RUNNING,
            )

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
