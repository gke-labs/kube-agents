from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from admin_console.connections import (
    CheckStatus,
    ClusterInfo,
    ConnectionCheck,
    ConnectionReport,
)
from admin_console.connection_persistence import (
    delete_connection,
    load_connection,
    save_connection,
)
from admin_console.connection_controller import (
    CONNECTION_CONTROLLER_KEY,
    ConnectionController,
)
from admin_console.tests.activity_fixtures import FixtureTelemetryProvider
from admin_console.agent_runtime import (
    CronSnapshot,
    AgentConversation,
    AgentCronExecution,
    AgentCronJob,
    AgentMessage,
    AgentTaskUpdate,
    HistoryResult,
    KanbanBoardResult,
    KanbanDelivery,
    KanbanEvent,
    KanbanRun,
    KanbanTaskDetail,
    KanbanTaskSummary,
    MessageResult,
    TaskUpdateResult,
)
from admin_console.agent_chat import ChatRunResult
from admin_console.project_config import DeploymentTarget
from admin_console.telemetry import TelemetrySnapshot, TelemetrySourceState

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_PATH = REPO_ROOT / "admin_console" / "app.py"
CONNECTION_PAGE_PATH = REPO_ROOT / "admin_console" / "pages" / "connections.py"
PROJECT = "test-project-01"
CLUSTER = "test-cluster-01"
LOCATION = "us-east4"
NAMESPACE = "kubeagents-system"
TARGET = DeploymentTarget(PROJECT, CLUSTER, LOCATION, NAMESPACE)


def connection_report(
    *,
    runtime_status: CheckStatus = CheckStatus.PASS,
    runtime_summary: str = "",
    runtime_guidance: str = "",
    audit_status: CheckStatus = CheckStatus.WARNING,
    clusters: tuple[ClusterInfo, ...] | None = None,
) -> ConnectionReport:
    checks = (
        ConnectionCheck("cli_auth", "gcloud CLI identity", CheckStatus.PASS, "Ready."),
        ConnectionCheck("adc", "ADC", CheckStatus.PASS, "Ready."),
        ConnectionCheck("project", "Project access", CheckStatus.PASS, "Ready."),
        ConnectionCheck("apis", "Required APIs", CheckStatus.PASS, "Ready."),
        ConnectionCheck("gke", "GKE discovery", CheckStatus.PASS, "Ready."),
        ConnectionCheck(
            "agent_runtime",
            "Agent runtime read",
            runtime_status,
            runtime_summary
            or ("Ready." if runtime_status == CheckStatus.PASS else "Unavailable."),
            runtime_guidance,
        ),
        ConnectionCheck("logging", "Cloud Logging", CheckStatus.PASS, "Ready."),
        ConnectionCheck("audit", "Audit events", audit_status, "No recent data."),
        ConnectionCheck("trace", "Cloud Trace", CheckStatus.PASS, "Ready."),
    )
    return ConnectionReport(
        PROJECT,
        datetime.now(UTC),
        checks,
        clusters
        if clusters is not None
        else (ClusterInfo(CLUSTER, LOCATION, "RUNNING", True),),
    )


class FakeHistoryProvider:
    def __init__(self, target: DeploymentTarget) -> None:
        self.target = target

    def list_agents(self) -> tuple[str, ...]:
        return ("test-agent-01",)

    def list_conversations(self, agent: str, *, cutoff, limit: int = 200) -> HistoryResult:
        conversation = AgentConversation(
            session_id="session-1",
            profile="default",
            platform="google_chat",
            user="user@example.com",
            attribution="attributed",
            title="Cluster investigation",
            preview="Please inspect the cluster.",
            chat_type="dm",
            chat_id="space-1",
            thread_id="",
            started_at=datetime(2026, 7, 30, 10, tzinfo=UTC),
            last_active=datetime(2026, 7, 30, 10, 1, tzinfo=UTC),
            message_count=2,
            tool_call_count=1,
        )
        return HistoryResult((conversation,), False)

    def get_messages(
        self,
        agent: str,
        *,
        profile: str,
        session_id: str,
        limit: int = 500,
    ) -> MessageResult:
        return MessageResult(
            (
                AgentMessage(
                    1,
                    "user",
                    "Please inspect the cluster.",
                    datetime(2026, 7, 30, 10, tzinfo=UTC),
                ),
                AgentMessage(
                    2,
                    "assistant",
                    "The cluster is healthy.",
                    datetime(2026, 7, 30, 10, 1, tzinfo=UTC),
                ),
            ),
            False,
        )

    def get_task_updates(
        self,
        agent: str,
        *,
        session_id: str,
        limit: int = 100,
    ) -> TaskUpdateResult:
        return TaskUpdateResult((), False)

    def list_kanban_tasks(self, agent: str, *, limit: int = 500) -> KanbanBoardResult:
        return KanbanBoardResult((self.kanban_task(),), False)

    def get_kanban_task(self, agent: str, task_id: str) -> KanbanTaskDetail:
        task = self.kanban_task()
        return KanbanTaskDetail(
            task=task,
            body="Inspect the cluster applications.",
            created_by="worker",
            started_at=datetime(2026, 8, 4, 10, tzinfo=UTC),
            completed_at=datetime(2026, 8, 4, 10, 1, tzinfo=UTC),
            workspace_kind="scratch",
            project_id=PROJECT,
            result="",
            block_kind="",
            current_step="",
            consecutive_failures=0,
            goal_mode=False,
            parents=(),
            children=(),
            runs=(
                KanbanRun(
                    1,
                    "platform",
                    "",
                    "done",
                    datetime(2026, 8, 4, 10, tzinfo=UTC),
                    datetime(2026, 8, 4, 10, 1, tzinfo=UTC),
                    "completed",
                    "Applications inspected.",
                    '{"count": 3}',
                    "",
                ),
            ),
            events=(
                KanbanEvent(
                    1,
                    1,
                    "completed",
                    '{"count": 3}',
                    datetime(2026, 8, 4, 10, 1, tzinfo=UTC),
                ),
            ),
            comments=(),
            attachments=(),
            deliveries=(
                KanbanDelivery(
                    "admin_portal",
                    True,
                    False,
                    True,
                    "default",
                    datetime(2026, 8, 4, 10, tzinfo=UTC),
                    1,
                ),
            ),
        )

    def get_cron_snapshot(
        self,
        agent: str,
        *,
        job_limit: int = 500,
        execution_limit: int = 500,
    ) -> CronSnapshot:
        read_at = datetime(2026, 8, 5, 19, tzinfo=UTC)
        jobs = (
            AgentCronJob(
                "default",
                "job-default",
                "Fleet inventory",
                True,
                "scheduled",
                "0 * * * *",
                "Inspect the fleet",
                "",
                "agent",
                datetime(2026, 8, 5, 18, tzinfo=UTC),
                datetime(2026, 8, 5, 20, tzinfo=UTC),
                "ok",
                "",
                "active",
                datetime(2026, 8, 5, 18, 59, tzinfo=UTC),
                "cron",
                "0 * * * *",
                0,
            ),
            AgentCronJob(
                "cluster-test-01",
                "job-visitors",
                "Unique visitors",
                True,
                "scheduled",
                "every 60m",
                "Count unique visitors",
                "count.py",
                "script",
                datetime(2026, 8, 5, 18, 30, tzinfo=UTC),
                datetime(2026, 8, 5, 19, 30, tzinfo=UTC),
                "ok",
                "",
                "missing",
                None,
                "interval",
                "",
                60,
            ),
        )
        executions = (
            AgentCronExecution(
                "execution-1",
                "cluster-test-01",
                "job-visitors",
                "direct",
                "completed",
                datetime(2026, 8, 5, 18, 29, tzinfo=UTC),
                datetime(2026, 8, 5, 18, 30, tzinfo=UTC),
                datetime(2026, 8, 5, 18, 30, 2, tzinfo=UTC),
                "",
            ),
            AgentCronExecution(
                "execution-2",
                "cluster-test-01",
                "job-visitors",
                "cron",
                "completed",
                datetime(2026, 8, 5, 17, 29, tzinfo=UTC),
                datetime(2026, 8, 5, 17, 30, tzinfo=UTC),
                datetime(2026, 8, 5, 17, 30, 3, tzinfo=UTC),
                "",
            ),
            AgentCronExecution(
                "execution-3",
                "cluster-test-01",
                "job-visitors",
                "cron",
                "failed",
                datetime(2026, 8, 5, 16, 29, tzinfo=UTC),
                datetime(2026, 8, 5, 16, 30, tzinfo=UTC),
                datetime(2026, 8, 5, 16, 30, 1, tzinfo=UTC),
                "upstream <unavailable>\nretry exhausted",
            ),
        )
        return CronSnapshot(jobs, executions, False, False, read_at)

    @staticmethod
    def kanban_task() -> KanbanTaskSummary:
        return KanbanTaskSummary(
            "t_12345678",
            "Inspect applications",
            "platform",
            "done",
            0,
            datetime(2026, 8, 4, 10, tzinfo=UTC),
            datetime(2026, 8, 4, 10, 1, tzinfo=UTC),
            "portal_saved",
            1,
            0,
            0,
            "Applications inspected.",
            "",
        )


class FakePaginatedHistoryProvider(FakeHistoryProvider):
    def list_conversations(
        self, agent: str, *, cutoff, limit: int = 200
    ) -> HistoryResult:
        base = super().list_conversations(
            agent, cutoff=cutoff, limit=limit
        ).conversations[0]
        conversations = tuple(
            replace(
                base,
                session_id=f"session-{index:02d}",
                title=(
                    "work kanban task t_00000000"
                    if index == 0
                    else f"Session {index:02d}"
                ),
                preview=f"Conversation {index:02d} preview",
            )
            for index in range(30)
        )
        return HistoryResult(conversations, False)

    def get_messages(
        self,
        agent: str,
        *,
        profile: str,
        session_id: str,
        limit: int = 500,
    ) -> MessageResult:
        return MessageResult(
            (
                AgentMessage(
                    1,
                    "assistant",
                    f"Transcript for {session_id}",
                    datetime(2026, 7, 30, 10, tzinfo=UTC),
                ),
            ),
            False,
        )

    def list_kanban_tasks(self, agent: str, *, limit: int = 500) -> KanbanBoardResult:
        base = self.kanban_task()
        tasks = tuple(
            replace(
                base,
                task_id=f"t_{index:08d}",
                title=f"Task {index:02d}",
                summary=f"Task {index:02d} completed.",
            )
            for index in range(30)
        )
        return KanbanBoardResult(tasks, False)

    def get_kanban_task(self, agent: str, task_id: str) -> KanbanTaskDetail:
        detail = super().get_kanban_task(agent, task_id)
        index = int(task_id.removeprefix("t_"))
        task = replace(
            detail.task,
            task_id=task_id,
            title=f"Task {index:02d}",
            summary=f"Task {index:02d} completed.",
        )
        return replace(detail, task=task, body=f"Details for {task_id}")


class FakePortalHistoryProvider(FakeHistoryProvider):
    def list_conversations(self, agent: str, *, cutoff, limit: int = 200) -> HistoryResult:
        conversation = AgentConversation(
            session_id="portal_saved",
            profile="default",
            platform="admin_portal",
            user="admin@example.com",
            attribution="attributed",
            title="Battleship deployment",
            preview="Deploy Battleship.",
            chat_type="dm",
            chat_id="",
            thread_id="",
            started_at=datetime(2026, 8, 4, 10, tzinfo=UTC),
            last_active=datetime(2026, 8, 4, 10, 1, tzinfo=UTC),
            message_count=2,
            tool_call_count=3,
        )
        return HistoryResult((conversation,), False)

    def get_task_updates(
        self,
        agent: str,
        *,
        session_id: str,
        limit: int = 100,
    ) -> TaskUpdateResult:
        task = AgentTaskUpdate(
            task_id="t_12345678",
            title="Inspect Battleship",
            assignee="platform",
            status="done",
            created_at=datetime(2026, 8, 4, 10, tzinfo=UTC),
            updated_at=datetime(2026, 8, 4, 10, 1, tzinfo=UTC),
            summary="Battleship is healthy.",
            error="",
            run_count=2,
            latest_event="completed",
            latest_event_at=datetime(2026, 8, 4, 10, 1, tzinfo=UTC),
            previous_error="First run crashed.",
        )
        return TaskUpdateResult((task,), False)


class FakeActiveTaskHistoryProvider(FakeHistoryProvider):
    def get_task_updates(
        self,
        agent: str,
        *,
        session_id: str,
        limit: int = 100,
    ) -> TaskUpdateResult:
        return TaskUpdateResult(
            (
                AgentTaskUpdate(
                    task_id="t_active123",
                    title="Deploy application",
                    assignee="platform",
                    status="running",
                    created_at=datetime(2026, 8, 5, 18, tzinfo=UTC),
                    updated_at=datetime(2026, 8, 5, 19, tzinfo=UTC),
                    summary="",
                    error="",
                    run_count=1,
                    latest_event="heartbeat",
                ),
            ),
            False,
        )


class FakeCompletingTaskHistoryProvider(FakeActiveTaskHistoryProvider):
    task_reads = 0

    def get_task_updates(
        self,
        agent: str,
        *,
        session_id: str,
        limit: int = 100,
    ) -> TaskUpdateResult:
        type(self).task_reads += 1
        result = super().get_task_updates(
            agent,
            session_id=session_id,
            limit=limit,
        )
        if type(self).task_reads == 1:
            return result
        task = result.tasks[0]
        return TaskUpdateResult(
            (
                AgentTaskUpdate(
                    task_id=task.task_id,
                    title=task.title,
                    assignee=task.assignee,
                    status="done",
                    created_at=task.created_at,
                    updated_at=datetime(2026, 8, 5, 19, 1, tzinfo=UTC),
                    summary="Application deployed.",
                    error="",
                    run_count=1,
                    latest_event="completed",
                ),
            ),
            False,
        )


class FakeReviewTaskHistoryProvider(FakeActiveTaskHistoryProvider):
    def get_task_updates(
        self,
        agent: str,
        *,
        session_id: str,
        limit: int = 100,
    ) -> TaskUpdateResult:
        result = super().get_task_updates(
            agent,
            session_id=session_id,
            limit=limit,
        )
        return TaskUpdateResult((replace(result.tasks[0], status="review"),), False)


class FakeChatProvider:
    def __init__(self, target: DeploymentTarget) -> None:
        self.target = target

    def run(self, agent: str, *, prompt: str, session_id: str, history=(), **kwargs):
        return ChatRunResult(
            "run_0123456789abcdef0123456789abcdef",
            session_id,
            "completed",
            f"Agent received: {prompt}",
        )


class FakeTelemetryProvider:
    def __init__(
        self,
        project_id: str,
        *,
        account: str = "",
        cluster: str = "",
        namespace: str = NAMESPACE,
        hours: int = 24,
        trace_pages: int = 1,
    ) -> None:
        self.project_id = project_id
        self.cluster = cluster
        self.namespace = namespace
        self.hours = hours
        self.trace_pages = trace_pages
        self.trace_limit = 100
        self.events = tuple(FixtureTelemetryProvider().list_activity())
        self._snapshot = None

    @property
    def loaded(self):
        return self._snapshot is not None

    @property
    def can_load_more(self):
        return self.trace_pages < 2

    def list_activity(self):
        return list(self.events)

    def get_snapshot(self) -> TelemetrySnapshot:
        if self._snapshot is not None:
            return self._snapshot
        end = datetime.now(UTC)
        self._snapshot = TelemetrySnapshot(
            self.project_id,
            self.cluster,
            end,
            end,
            end,
            self.events,
            (
                TelemetrySourceState(
                    "Cloud Trace",
                    "ready",
                    len(self.events),
                    self.trace_pages < 2,
                    "Deterministic functional-test data.",
                    can_load_more=self.trace_pages < 2,
                ),
            ),
        )
        return self._snapshot

    def load_more(self):
        self.trace_pages = 2
        self._snapshot = None
        return self.get_snapshot()


class FakeLargeTelemetryProvider(FakeTelemetryProvider):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        source = self.events[0]
        self.events = tuple(
            replace(
                source,
                event_id=f"event-{index:03d}",
                interaction_id=f"interaction-{index:03d}",
                occurred_at=source.occurred_at - timedelta(seconds=index),
            )
            for index in range(105)
        )
        self.trace_pages = 2


class AdminPortalFunctionalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(
            os.environ,
            {
                "KUBE_AGENTS_ADMIN_USER": "admin@example.com",
                "KUBE_AGENTS_GCLOUD_PROJECT": PROJECT,
                "KUBE_AGENTS_ADMIN_CONNECTION_STATE": str(
                    Path(self.temporary_directory.name) / "connection.json"
                ),
            },
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary_directory.cleanup()

    def app(self, *, connected: bool = False) -> AppTest:
        app = AppTest.from_file(APP_PATH, default_timeout=20)
        app.query_params.update(
            {"project": PROJECT, "cluster": CLUSTER, "location": LOCATION}
        )
        if connected:
            app.session_state[CONNECTION_CONTROLLER_KEY] = ConnectionController.verified(
                "admin@example.com", TARGET, connection_report()
            )
        return app

    def controller(self, app: AppTest) -> ConnectionController:
        return app.session_state[CONNECTION_CONTROLLER_KEY]

    def finish_connection_job(self, app: AppTest) -> AppTest:
        """Wait on the dependency itself, then let the UI consume its result."""
        if CONNECTION_CONTROLLER_KEY not in app.session_state:
            app = app.run()
        controller = self.controller(app)
        if controller.job is None:
            return app
        job = controller.job
        job.future.result(timeout=20)
        return app.run()

    def connect_project(self, app: AppTest) -> AppTest:
        next(
            button
            for button in app.button
            if button.key == "project_connection_primary_disconnected"
        ).click().run()
        return self.finish_connection_job(app)

    def connect_cluster(self, app: AppTest) -> AppTest:
        next(
            button
            for button in app.button
            if button.key == "cluster_connection_primary_disconnected"
        ).click().run()
        return self.finish_connection_job(app)

    def test_provider_backed_pages_stay_visible_before_connection(self):
        for page, title in (
            ("pages/chat.py", "Chat"),
            ("pages/overview.py", "Overview"),
            ("pages/activity.py", "Activity Explorer"),
            ("pages/kanban.py", "Task Kanban"),
            ("pages/autonomous.py", "Scheduled Cron"),
        ):
            with self.subTest(page=page):
                app = self.app().run().switch_page(page).run()

                self.assertEqual(len(app.exception), 0)
                self.assertEqual(app.title[0].value, title)
                self.assertEqual(
                    app.info[0].value,
                    "Connect a project and cluster on Connection.",
                )

    def test_connection_page_bootstraps_when_streamlit_resumes_it_directly(self):
        app = AppTest.from_file(CONNECTION_PAGE_PATH, default_timeout=20)
        app.query_params.update(
            {"project": PROJECT, "cluster": CLUSTER, "location": LOCATION}
        )
        app = app.run()

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.title[0].value, "Connection")
        self.assertIsInstance(self.controller(app), ConnectionController)
        self.assertTrue(
            any(
                "Signed in as admin@example.com" in item.value
                for item in app.sidebar.caption
            )
        )
        self.assertTrue(
            any(
                button.key == "project_connection_primary_disconnected"
                for button in app.button
            )
        )

    def test_project_and_cluster_connect_independently(self):
        with patch(
            "admin_console.connections.run_connection_checks",
            return_value=connection_report(audit_status=CheckStatus.FAIL),
        ) as run_checks:
            app = self.connect_project(self.app().run())

            self.assertIsNone(self.controller(app).connected_target)
            self.assertEqual(self.controller(app).connected_project, PROJECT)
            self.assertTrue(
                next(
                    button
                    for button in app.button
                    if button.key == "project_connection_primary_connected"
                ).disabled
            )
            self.assertFalse(
                next(
                    button
                    for button in app.button
                    if button.key == "cluster_connection_primary_disconnected"
                ).disabled
            )

            app = self.connect_cluster(app)

        connected = self.controller(app).connected_target
        self.assertEqual(
            (connected.project_id, connected.cluster_name, connected.location),
            (PROJECT, CLUSTER, LOCATION),
        )
        self.assertEqual(run_checks.call_count, 2)
        self.assertFalse(
            run_checks.call_args_list[0].kwargs["include_agent_runtime_probe"]
        )
        self.assertFalse(
            run_checks.call_args_list[0].kwargs["include_telemetry_probes"]
        )
        self.assertIsNone(run_checks.call_args_list[0].kwargs["expected_target"])
        self.assertTrue(
            run_checks.call_args_list[1].kwargs["include_agent_runtime_probe"]
        )
        self.assertTrue(
            run_checks.call_args_list[1].kwargs["include_telemetry_probes"]
        )
        expected_target = run_checks.call_args_list[1].kwargs["expected_target"]
        self.assertEqual(
            (
                expected_target.project_id,
                expected_target.cluster_name,
                expected_target.location,
                expected_target.namespace,
            ),
            (PROJECT, CLUSTER, LOCATION, NAMESPACE),
        )
        self.assertEqual(
            sum(button.label == "Connected" for button in app.button),
            2,
        )

    def test_cluster_connection_is_shared_with_task_kanban(self):
        with patch(
            "admin_console.connections.run_connection_checks",
            return_value=connection_report(),
        ), patch(
            "admin_console.agent_runtime.AgentRuntimeProvider",
            FakeHistoryProvider,
        ):
            app = self.connect_project(self.app().run())
            app = self.connect_cluster(app)
            app = app.switch_page("pages/kanban.py").run()

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.info), 0)
        self.assertEqual(app.title[0].value, "Task Kanban")
        connected = self.controller(app).connected_target
        self.assertEqual(
            (connected.project_id, connected.cluster_name, connected.location),
            (PROJECT, CLUSTER, LOCATION),
        )
        self.assertTrue(
            any("Applications inspected" in item.value for item in app.success)
        )

    def test_saved_connection_resumes_with_live_revalidation(self):
        verified_at = datetime.now(UTC)
        save_connection("admin@example.com", TARGET, verified_at)
        with patch(
            "admin_console.connections.run_connection_checks",
            return_value=connection_report(),
        ) as run_checks:
            app = AppTest.from_file(APP_PATH, default_timeout=20).run()
            app = self.finish_connection_job(app)

        self.assertEqual(self.controller(app).connected_target, TARGET)
        self.assertEqual(app.query_params["project"], [PROJECT])
        self.assertEqual(app.query_params["cluster"], [CLUSTER])
        self.assertEqual(app.query_params["location"], [LOCATION])
        self.assertEqual([button.label for button in app.button].count("Connected"), 2)
        self.assertEqual(len(app.selectbox), 2)
        self.assertTrue(
            any("Step 1 · Project" in item.value for item in app.markdown)
        )
        self.assertTrue(
            any("Step 2 · Cluster" in item.value for item in app.markdown)
        )
        run_checks.assert_called_once()
        self.assertEqual(run_checks.call_args.kwargs["expected_target"], TARGET)
        self.assertTrue(run_checks.call_args.kwargs["include_agent_runtime_probe"])

    def test_suspended_lease_stays_locked_until_revalidation_finishes(self):
        release_check = Event()

        def blocked_check(*args, **kwargs):
            release_check.wait(timeout=20)
            return connection_report()

        save_connection(
            "admin@example.com",
            TARGET,
            datetime.now(UTC),
            usable=False,
        )
        with patch(
            "admin_console.connections.run_connection_checks",
            side_effect=blocked_check,
        ):
            app = AppTest.from_file(APP_PATH, default_timeout=20).run()
            self.assertIsNone(self.controller(app).connected_target)
            job = self.controller(app).job
            self.assertIsNotNone(job)
            release_check.set()
            job.future.result(timeout=20)
            app = app.run()

        self.assertEqual(self.controller(app).connected_target, TARGET)

    def test_selected_target_without_saved_lease_does_not_auto_connect(self):
        with patch(
            "admin_console.connections.run_connection_checks",
            return_value=connection_report(),
        ) as run_checks:
            app = self.app().run()

        self.assertIsNone(self.controller(app).connected_project)
        self.assertIsNone(self.controller(app).connected_target)
        run_checks.assert_not_called()

    def test_project_connect_shows_matching_abort_pair(self):
        release_check = Event()

        def blocked_check(*args, **kwargs):
            release_check.wait(timeout=20)
            return connection_report()

        with patch(
            "admin_console.connections.run_connection_checks",
            side_effect=blocked_check,
        ):
            app = self.app().run()
            next(
                button
                for button in app.button
                if button.key == "project_connection_primary_disconnected"
            ).click().run()

            connecting = next(
                button
                for button in app.button
                if button.key == "project_connection_primary_connecting"
            )
            self.assertTrue(connecting.disabled)
            abort = next(
                button
                for button in app.button
                if button.key == "project_connection_secondary_abort"
            )
            self.assertFalse(abort.disabled)
            self.assertTrue(
                any(
                    "Connecting to the selected project" in item.label
                    for item in app.sidebar.status
                )
            )

            release_check.set()
            app = self.finish_connection_job(app)

        self.assertEqual(self.controller(app).connected_project, PROJECT)
        self.assertIsNone(self.controller(app).connected_target)

    def test_project_abort_ignores_a_late_result(self):
        release_check = Event()

        def blocked_check(*args, **kwargs):
            release_check.wait(timeout=20)
            return connection_report()

        with patch(
            "admin_console.connections.run_connection_checks",
            side_effect=blocked_check,
        ):
            app = self.app().run()
            next(
                button
                for button in app.button
                if button.key == "project_connection_primary_disconnected"
            ).click().run()
            next(
                button
                for button in app.button
                if button.key == "project_connection_secondary_abort"
            ).click().run()

            self.assertIsNone(self.controller(app).connected_project)
            self.assertIsNone(self.controller(app).connected_target)
            self.assertIsNone(self.controller(app).job)
            self.assertFalse(
                next(
                    button
                    for button in app.button
                    if button.key == "project_connection_primary_disconnected"
                ).disabled
            )

            release_check.set()
            app.run()

        self.assertIsNone(self.controller(app).connected_project)

    def test_cluster_abort_keeps_project_connected(self):
        release_check = Event()

        def connection_check(*args, expected_target=None, **kwargs):
            if expected_target is None:
                return connection_report()
            release_check.wait(timeout=20)
            return connection_report()

        with patch(
            "admin_console.connections.run_connection_checks",
            side_effect=connection_check,
        ):
            app = self.connect_project(self.app().run())
            next(
                button
                for button in app.button
                if button.key == "cluster_connection_primary_disconnected"
            ).click().run()
            next(
                button
                for button in app.button
                if button.key == "cluster_connection_secondary_abort"
            ).click().run()
            release_check.set()
            app.run()

        self.assertEqual(self.controller(app).connected_project, PROJECT)
        self.assertIsNone(self.controller(app).connected_target)

    def test_failed_revalidation_disconnects_only_the_cluster(self):
        app = self.app(connected=True)
        self.controller(app).verified_at = datetime(2020, 1, 1, tzinfo=UTC)
        save_connection(
            "admin@example.com",
            TARGET,
            self.controller(app).verified_at,
        )
        with patch(
            "admin_console.connections.run_connection_checks",
            return_value=connection_report(runtime_status=CheckStatus.FAIL),
        ):
            app = app.run()
            app = self.finish_connection_job(app)

        self.assertIsNone(self.controller(app).connected_target)
        self.assertEqual(self.controller(app).connected_project, PROJECT)
        saved = load_connection("admin@example.com")
        self.assertIsNotNone(saved)
        self.assertEqual(saved.target, TARGET)
        self.assertFalse(saved.usable)
        self.assertTrue(
            any("Agent runtime read: Unavailable" in item.value for item in app.error)
        )

    def test_revalidation_exception_retains_suspended_target(self):
        app = self.app(connected=True)
        self.controller(app).verified_at = datetime(2020, 1, 1, tzinfo=UTC)
        save_connection(
            "admin@example.com",
            TARGET,
            self.controller(app).verified_at,
        )
        with patch(
            "admin_console.connections.run_connection_checks",
            side_effect=RuntimeError("probe crashed"),
        ):
            app = app.run()

        self.assertIsNone(self.controller(app).connected_target)
        saved = load_connection("admin@example.com")
        self.assertIsNotNone(saved)
        self.assertEqual(saved.target, TARGET)
        self.assertFalse(saved.usable)
        self.assertTrue(
            any("stopped unexpectedly" in item.value for item in app.error)
        )

    def test_refresh_shows_disconnect_and_late_result_cannot_reconnect(self):
        release_check = Event()

        def blocked_check(*args, **kwargs):
            release_check.wait(timeout=20)
            return connection_report()

        app = self.app(connected=True)
        save_connection("admin@example.com", TARGET, datetime.now(UTC))
        self.controller(app).verified_at = datetime(2020, 1, 1, tzinfo=UTC)
        with patch(
            "admin_console.connections.run_connection_checks",
            side_effect=blocked_check,
        ):
            app = app.run()
            self.assertEqual([button.label for button in app.button].count("Abort"), 0)
            disconnect = next(
                button
                for button in app.button
                if button.key == "cluster_connection_secondary_disconnect"
            )
            self.assertFalse(disconnect.disabled)
            app = disconnect.click().run()
            self.assertIsNone(self.controller(app).job)
            self.assertIsNone(self.controller(app).connected_target)
            self.assertIsNone(load_connection("admin@example.com"))
            release_check.set()
            app = app.run()

        self.assertIsNone(self.controller(app).connected_target)

    def test_other_tab_disconnect_detaches_persisted_session(self):
        app = self.app(connected=True)
        controller = self.controller(app)
        save_connection("admin@example.com", TARGET, datetime.now(UTC))
        controller.persisted_lease = True
        app = app.run()
        self.assertEqual(self.controller(app).connected_target, TARGET)

        delete_connection()
        app = app.run()

        self.assertIsNone(self.controller(app).connected_target)
        self.assertTrue(
            any("changed in another portal tab" in item.value for item in app.error)
        )

    def test_other_tab_disconnect_cancels_suspended_lease_revalidation(self):
        release_check = Event()

        def blocked_check(*args, **kwargs):
            release_check.wait(timeout=20)
            return connection_report()

        save_connection(
            "admin@example.com",
            TARGET,
            datetime.now(UTC),
            usable=False,
        )
        with patch(
            "admin_console.connections.run_connection_checks",
            side_effect=blocked_check,
        ):
            app = AppTest.from_file(APP_PATH, default_timeout=20).run()
            self.assertIsNotNone(self.controller(app).job)
            delete_connection()
            app = app.run()
            release_check.set()
            app = app.run()

        self.assertIsNone(self.controller(app).job)
        self.assertIsNone(self.controller(app).connected_target)
        self.assertIsNone(load_connection("admin@example.com"))

    def test_unique_host_is_preselected_but_not_auto_connected(self):
        detected_cluster = "detected-host"
        report = connection_report(
            clusters=(ClusterInfo(detected_cluster, LOCATION, "RUNNING", True),)
        )
        with patch(
            "admin_console.connections.run_connection_checks",
            return_value=report,
        ) as run_checks:
            app = self.connect_project(self.app().run())

        self.assertIsNone(self.controller(app).connected_target)
        self.assertEqual(app.query_params["cluster"], [detected_cluster])
        cluster = next(item for item in app.selectbox if item.label == "Cluster")
        self.assertEqual(cluster.value, f"{detected_cluster}|{LOCATION}")
        self.assertIsNone(run_checks.call_args.kwargs["expected_target"])

    def test_project_selector_accepts_custom_project_without_second_field(self):
        with patch(
            "admin_console.connection_sidebar.st.selectbox",
            return_value="custom-project-01",
        ):
            app = self.app().run()

        self.assertFalse(any(item.label == "Project ID" for item in app.text_input))
        self.assertEqual(self.controller(app).project_id, "custom-project-01")
        self.assertEqual(app.query_params["project"], ["custom-project-01"])

    def test_invalid_custom_project_disables_connect(self):
        with patch(
            "admin_console.connection_sidebar.st.selectbox",
            return_value="INVALID PROJECT",
        ):
            app = self.app().run()

        self.assertTrue(
            any("valid Google Cloud project ID" in item.value for item in app.error)
        )
        connect = next(
            button
            for button in app.button
            if button.key == "project_connection_primary_disconnected"
        )
        self.assertTrue(connect.disabled)

    def test_cluster_picker_handles_missing_host_label_without_an_error(self):
        clusters = (
            ClusterInfo("cluster-a", LOCATION, "RUNNING"),
            ClusterInfo("cluster-b", LOCATION, "RUNNING"),
        )
        discovery = connection_report(clusters=clusters)
        with patch(
            "admin_console.connections.run_connection_checks",
            return_value=discovery,
        ):
            app = self.connect_project(self.app().run())

            self.assertIsNone(self.controller(app).connected_target)
            self.assertEqual(len(app.error), 0)
            self.assertTrue(
                any("No kube-agents host label" in item.value for item in app.caption)
            )
            cluster_select = next(
                item for item in app.selectbox if item.label == "Cluster"
            )
            self.assertEqual(
                cluster_select.options,
                ["cluster-a · us-east4", "cluster-b · us-east4"],
            )

    def test_cluster_connect_shows_matching_abort_pair(self):
        clusters = (
            ClusterInfo("cluster-a", LOCATION, "RUNNING"),
            ClusterInfo("cluster-b", LOCATION, "RUNNING"),
        )
        discovery = connection_report(clusters=clusters)
        verified = connection_report(clusters=clusters)
        release_check = Event()

        def connection_check(*args, expected_target=None, **kwargs):
            if expected_target is None:
                return discovery
            release_check.wait(timeout=20)
            return verified

        with patch(
            "admin_console.connections.run_connection_checks",
            side_effect=connection_check,
        ):
            app = self.app().run()
            app = self.connect_project(self.app().run())
            cluster_select = next(
                item for item in app.selectbox if item.label == "Cluster"
            )
            cluster_select.select("cluster-b|us-east4").run()
            next(
                button
                for button in app.button
                if button.key == "cluster_connection_primary_disconnected"
            ).click().run()

            connecting = next(
                button
                for button in app.button
                if button.key == "cluster_connection_primary_connecting"
            )
            self.assertTrue(connecting.disabled)
            self.assertFalse(
                next(
                    button
                    for button in app.button
                    if button.key == "cluster_connection_secondary_abort"
                ).disabled
            )
            self.assertEqual(len(app.error), 0)
            self.assertTrue(
                any(
                    "Connecting to the selected cluster" in item.label
                    for item in app.sidebar.status
                )
            )
            release_check.set()
            app = self.finish_connection_job(app)

        self.assertEqual(self.controller(app).connected_target.cluster_name, "cluster-b")

    def test_multiple_host_labels_use_the_same_cluster_picker(self):
        report = connection_report(
            clusters=(
                ClusterInfo("host-a", LOCATION, "RUNNING", True),
                ClusterInfo("host-b", "us-central1", "RUNNING", True),
            )
        )
        with patch(
            "admin_console.connections.run_connection_checks",
            return_value=report,
        ):
            app = self.connect_project(self.app().run())

        self.assertIsNone(self.controller(app).connected_target)
        self.assertEqual(len(app.error), 0)
        self.assertTrue(
            any("Multiple host labels" in item.value for item in app.caption)
        )

    def test_project_without_clusters_connects_but_disables_cluster_connect(self):
        report = connection_report(clusters=())
        with patch(
            "admin_console.connections.run_connection_checks",
            return_value=report,
        ):
            app = self.connect_project(self.app().run())

        self.assertEqual(self.controller(app).connected_project, PROJECT)
        self.assertIsNone(self.controller(app).connected_target)
        self.assertTrue(
            next(
                button
                for button in app.button
                if button.key == "cluster_connection_primary_disconnected"
            ).disabled
        )
        self.assertTrue(any("No GKE clusters" in item.value for item in app.info))

    def test_failed_cluster_connection_keeps_project_connected(self):
        with patch(
            "admin_console.connections.run_connection_checks",
            side_effect=(
                connection_report(),
                connection_report(
                    runtime_status=CheckStatus.FAIL,
                    runtime_summary="No running kube-agents gateway pods were found.",
                    runtime_guidance="Confirm the selected cluster and namespace.",
                ),
            ),
        ):
            app = self.connect_project(self.app().run())
            app = self.connect_cluster(app)

        self.assertEqual(self.controller(app).connected_project, PROJECT)
        self.assertIsNone(self.controller(app).connected_target)
        self.assertEqual(len(app.error), 1)
        self.assertEqual(
            app.error[0].value,
            "Could not connect to cluster test-cluster-01. Agent runtime read: "
            "No running kube-agents gateway pods were found. "
            "Confirm the selected cluster and namespace.",
        )
        self.assertEqual(len(app.metric), 0)

    def test_cluster_disconnect_keeps_project_connected(self):
        app = self.app(connected=True).run()
        self.assertEqual([button.label for button in app.button].count("Disconnect"), 1)
        next(
            button
            for button in app.button
            if button.key == "cluster_connection_secondary_disconnect"
        ).click().run()

        self.assertEqual(self.controller(app).connected_project, PROJECT)
        self.assertIsNone(self.controller(app).connected_target)
        self.assertEqual(app.query_params["project"], [PROJECT])
        self.assertEqual(app.query_params["cluster"], [CLUSTER])
        self.assertEqual([button.label for button in app.button].count("Disconnect"), 1)

    def test_nested_disconnects_expose_only_one_action_at_a_time(self):
        app = self.app(connected=True).run()
        next(
            button
            for button in app.button
            if button.key == "cluster_connection_secondary_disconnect"
        ).click().run()
        self.assertEqual([button.label for button in app.button].count("Disconnect"), 1)
        next(
            button
            for button in app.button
            if button.key == "project_connection_secondary_disconnect"
        ).click().run()

        self.assertIsNone(self.controller(app).connected_project)
        self.assertIsNone(self.controller(app).connected_target)
        self.assertEqual(app.query_params["project"], [PROJECT])
        self.assertNotIn("cluster", app.query_params)
        self.assertEqual(len(app.error), 0)
        self.assertFalse(
            next(
                button
                for button in app.button
                if button.key == "project_connection_primary_disconnected"
            ).disabled
        )

    def test_chat_renders_real_projection_and_persists_safe_url_state(self):
        with patch(
            "admin_console.agent_runtime.AgentRuntimeProvider",
            FakeHistoryProvider,
        ):
            app = self.app(connected=True).run().switch_page("pages/chat.py").run()

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.chat_message), 2)
        self.assertEqual(
            [item.label for item in app.selectbox],
            [
                "Agent",
                "History",
                "Source",
            ],
        )
        agent_selector = next(item for item in app.selectbox if item.label == "Agent")
        self.assertIn("PlatformAgent custom resource", agent_selector.help)
        self.assertEqual(len(app.dataframe), 1)
        self.assertEqual(
            list(app.dataframe[0].value.columns),
            [
                "Last active",
                "Source",
                "User",
                "Session",
                "Messages",
                "Tools",
            ],
        )
        self.assertEqual(
            app.dataframe[0].value.iloc[0]["Session"],
            "Cluster investigation",
        )
        self.assertEqual(app.query_params["chat_agent"], ["test-agent-01"])
        self.assertEqual(app.query_params["chat_window"], ["all"])
        self.assertEqual(app.query_params["chat_session"], ["default:session-1"])
        self.assertNotIn("user@example.com", str(app.query_params))
        self.assertTrue(app.chat_input[0].disabled)
        self.assertTrue(
            any(button.label == "Start portal follow-up" for button in app.button)
        )

    def test_chat_paginates_conversations_and_persists_the_page(self):
        with patch(
            "admin_console.agent_runtime.AgentRuntimeProvider",
            FakePaginatedHistoryProvider,
        ):
            app = self.app(connected=True).run().switch_page("pages/chat.py").run()
            self.assertEqual(
                app.dataframe[0].value.iloc[0]["Session"],
                "Task Kanban · t_00000000",
            )
            next(
                button for button in app.button if button.label == "Next"
            ).click().run()

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.query_params["chat_page"], ["2"])
        self.assertEqual(app.query_params["chat_session"], ["default:session-25"])
        self.assertEqual(len(app.dataframe[0].value), 5)
        self.assertTrue(
            any("Transcript for session-25" in item.value for item in app.markdown)
        )
        pagination = {button.label: button for button in app.button}
        self.assertFalse(pagination["Previous"].disabled)
        self.assertTrue(pagination["Next"].disabled)

    def test_chat_polls_active_external_work_and_links_to_kanban(self):
        with patch(
            "admin_console.agent_runtime.AgentRuntimeProvider",
            FakeActiveTaskHistoryProvider,
        ):
            app = self.app(connected=True).run().switch_page("pages/chat.py").run()

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.status), 1)
        self.assertIn("Watching agent work", app.status[0].label)
        polling = dict(app.session_state.portal_task_polling)
        self.assertTrue(next(iter(polling.values())))
        task_link = next(
            item
            for item in app.get("page_link")
            if item.proto.label == "t_active123"
        )
        self.assertIn("kanban_task=t_active123", task_link.proto.query_string)

    def test_chat_stops_polling_when_linked_work_finishes(self):
        FakeCompletingTaskHistoryProvider.task_reads = 0
        with patch(
            "admin_console.agent_runtime.AgentRuntimeProvider",
            FakeCompletingTaskHistoryProvider,
        ):
            app = self.app(connected=True).run().switch_page("pages/chat.py").run()
            for _ in range(3):
                app = app.run()

        self.assertEqual(len(app.exception), 0)
        self.assertGreaterEqual(FakeCompletingTaskHistoryProvider.task_reads, 2)
        self.assertFalse(
            next(iter(dict(app.session_state.portal_task_polling).values()))
        )
        self.assertTrue(
            any("Application deployed" in item.value for item in app.success)
        )
        self.assertEqual(len(app.status), 0)

    def test_chat_keeps_polling_every_server_active_task_status(self):
        with patch(
            "admin_console.agent_runtime.AgentRuntimeProvider",
            FakeReviewTaskHistoryProvider,
        ):
            app = self.app(connected=True).run().switch_page("pages/chat.py").run()

        self.assertEqual(len(app.exception), 0)
        self.assertTrue(
            next(iter(dict(app.session_state.portal_task_polling).values()))
        )
        self.assertTrue(any("review" in item.value for item in app.markdown))

    def test_connected_cluster_survives_navigation_query_reset(self):
        provisioned_default = DeploymentTarget(
            PROJECT,
            "different-provisioned-cluster",
            LOCATION,
            NAMESPACE,
        )
        with patch(
            "admin_console.project_config.load_provisioned_target",
            return_value=provisioned_default,
        ), patch(
            "admin_console.agent_runtime.AgentRuntimeProvider",
            FakeHistoryProvider,
        ):
            app = self.app(connected=True).run()
            app.query_params.clear()
            app = app.switch_page("pages/chat.py").run()

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(self.controller(app).selected_target.cluster_name, CLUSTER)
        self.assertEqual(app.query_params["cluster"], [CLUSTER])
        self.assertEqual(len(app.chat_message), 2)

    def test_portal_conversation_rehydrates_from_url_and_remains_writable(self):
        with patch(
            "admin_console.agent_runtime.AgentRuntimeProvider",
            FakePortalHistoryProvider,
        ):
            app = self.app(connected=True)
            app.query_params["chat_session"] = "default:portal_saved"
            app = app.run().switch_page("pages/chat.py").run()

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.chat_message), 2)
        self.assertEqual(app.query_params["chat_session"], ["default:portal_saved"])
        self.assertFalse(app.chat_input[0].disabled)
        self.assertTrue(
            any("Battleship is healthy" in item.value for item in app.success)
        )
        self.assertTrue(
            any("Completed after 2 runs" in item.value for item in app.caption)
        )
        task_link = next(
            item
            for item in app.get("page_link")
            if item.proto.label == "t_12345678"
        )
        self.assertEqual(task_link.proto.page, "kanban")
        self.assertIn("kanban_agent=test-agent-01", task_link.proto.query_string)
        self.assertIn("kanban_task=t_12345678", task_link.proto.query_string)

    def test_chat_sends_a_real_run_and_renders_response(self):
        with patch(
            "admin_console.agent_runtime.AgentRuntimeProvider",
            FakeHistoryProvider,
        ), patch(
            "admin_console.agent_chat.AgentChatProvider",
            FakeChatProvider,
        ):
            app = self.app(connected=True).run().switch_page("pages/chat.py").run()
            next(button for button in app.button if button.label == "New chat").click().run()
            app.chat_input[0].set_value("Build Battleship").run()

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(
            [message.name for message in app.chat_message], ["user", "assistant"]
        )
        self.assertTrue(
            any("Agent received: Build Battleship" in item.value for item in app.markdown)
        )

    def test_kanban_renders_live_board_and_task_evidence(self):
        with patch(
            "admin_console.agent_runtime.AgentRuntimeProvider",
            FakeHistoryProvider,
        ):
            app = self.app(connected=True).run().switch_page("pages/kanban.py").run()

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.title[0].value, "Task Kanban")
        self.assertEqual(app.query_params["kanban_task"], ["t_12345678"])
        self.assertTrue(
            any("Applications inspected" in item.value for item in app.success)
        )
        self.assertNotIn("Inspect task", [item.label for item in app.selectbox])
        self.assertIn("selection_mode: SINGLE_ROW", str(app.dataframe[0].proto))

    def test_kanban_paginates_tasks_and_selects_page_details(self):
        with patch(
            "admin_console.agent_runtime.AgentRuntimeProvider",
            FakePaginatedHistoryProvider,
        ):
            app = self.app(connected=True).run().switch_page("pages/kanban.py").run()
            next(
                button for button in app.button if button.label == "Next"
            ).click().run()

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.query_params["kanban_page"], ["2"])
        self.assertEqual(app.query_params["kanban_task"], ["t_00000025"])
        self.assertEqual(len(app.dataframe[0].value), 5)
        self.assertTrue(any(item.value == "Task 25" for item in app.subheader))
        self.assertTrue(
            any("Details for t_00000025" in item.value for item in app.markdown)
        )
        pagination = {button.label: button for button in app.button}
        self.assertFalse(pagination["Previous"].disabled)
        self.assertTrue(pagination["Next"].disabled)

    def test_each_activity_page_owns_its_scope_controls(self):
        for page, title in (
            ("pages/overview.py", "Overview"),
            ("pages/activity.py", "Activity Explorer"),
        ):
            with self.subTest(page=page), patch(
                "admin_console.activity_scope.CloudTelemetryProvider",
                FakeTelemetryProvider,
            ):
                app = self.app(connected=True).run().switch_page(page).run()
                self.assertEqual(len(app.exception), 0)
                self.assertEqual(app.title[0].value, title)
                labels = [item.label for item in app.selectbox]
                self.assertIn("Time window", labels)
                self.assertIn("Cluster", labels)
                self.assertEqual(len(app.sidebar.selectbox), 0)

    def test_activity_ledger_paginates_fifty_events_at_a_time(self):
        with patch(
            "admin_console.activity_scope.CloudTelemetryProvider",
            FakeLargeTelemetryProvider,
        ):
            app = self.app(connected=True).run().switch_page("pages/activity.py").run()
            self.assertEqual(len(app.dataframe[0].value), 50)
            next(button for button in app.button if button.label == "Next").click().run()

        self.assertEqual(app.query_params["activity_page"], ["2"])
        self.assertEqual(len(app.dataframe[0].value), 50)
        self.assertTrue(
            any("51–100 of 105" in item.value for item in app.caption)
        )

    def test_activity_load_more_appends_the_next_source_pages(self):
        with patch(
            "admin_console.activity_scope.CloudTelemetryProvider",
            FakeTelemetryProvider,
        ):
            app = self.app(connected=True).run().switch_page("pages/activity.py").run()
            next(
                button
                for button in app.button
                if button.label == "Load more activity"
            ).click().run()

        self.assertEqual(len(app.exception), 0)
        self.assertFalse(
            any(button.label == "Load more activity" for button in app.button)
        )

    def test_scheduled_cron_renders_live_jobs_runs_and_calendar(self):
        with patch(
            "admin_console.agent_runtime.AgentRuntimeProvider",
            FakeHistoryProvider,
        ):
            app = (
                self.app(connected=True)
                .run()
                .switch_page("pages/autonomous.py")
                .run()
            )

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.title[0].value, "Scheduled Cron")
        self.assertEqual(app.query_params["cron_agent"], ["test-agent-01"])
        self.assertEqual(app.query_params["cron_window"], ["7d"])
        self.assertEqual(len(app.dataframe), 1)
        self.assertIn("Scheduler", app.dataframe[0].value.columns)
        execution_markup = next(
            item.proto.body
            for item in app.get("html")
            if "ka-cron-table-wrap" in item.proto.body
        )
        self.assertEqual(execution_markup.count("<tr>"), 2)
        self.assertIn("Unique visitors", execution_markup)
        self.assertIn("<summary>3 runs</summary>", execution_markup)
        self.assertIn("2026-08-05 18:30 UTC", execution_markup)
        self.assertIn("2026-08-05 17:30 UTC", execution_markup)
        self.assertIn("2026-08-05 16:30 UTC", execution_markup)
        self.assertIn("Manual", execution_markup)
        self.assertIn("upstream &lt;unavailable&gt;", execution_markup)
        self.assertIn("&gt;<br>retry exhausted", execution_markup)
        self.assertTrue(
            any("without a live scheduler" in item.value for item in app.warning)
        )
        self.assertTrue(any("ka-calendar" in item.value for item in app.markdown))
        calendar_markup = "".join(item.value for item in app.markdown)
        self.assertGreater(calendar_markup.count("Fleet inventory"), 1)
        self.assertIn("24 runs", calendar_markup)
        self.assertIn("scheduler unavailable", calendar_markup)

    def test_page_imports_work_outside_repository_cwd(self):
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            try:
                os.chdir(temporary_directory)
                app = self.app().run().switch_page("pages/connections.py").run()
            finally:
                os.chdir(original_cwd)

        self.assertEqual(len(app.exception), 0)


if __name__ == "__main__":
    unittest.main()
