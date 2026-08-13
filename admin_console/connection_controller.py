"""Single-source connection state machine shared by every portal page."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from threading import Event

from admin_console import connections
from admin_console.connection_persistence import (
    delete_connection,
    load_connection,
    save_connection,
)
from admin_console.project_config import DeploymentTarget, ProjectCandidate

CONNECTION_CONTROLLER_KEY = "connection_controller"
CONNECTION_REFRESH_INTERVAL = timedelta(minutes=10)


class ConnectionPhase(StrEnum):
    """A level's complete UI lifecycle."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"


class ConnectionAction(StrEnum):
    PROJECT = "project"
    CLUSTER = "cluster"
    REFRESH = "refresh"


@dataclass
class ConnectionLevel:
    phase: ConnectionPhase = ConnectionPhase.DISCONNECTED
    report: connections.ConnectionReport | None = None
    error: str = ""


@dataclass(frozen=True)
class ConnectionEvent:
    action: ConnectionAction
    outcome: str
    message: str = ""


@dataclass(frozen=True)
class ConnectionJob:
    action: ConnectionAction
    project_id: str
    target: DeploymentTarget | None
    future: Future[connections.ConnectionReport]
    cancel_event: Event


@dataclass
class ConnectionController:
    """Own connection selection, transitions, reports, and verified scope.

    This object is the only connection value stored in Streamlit session state.
    URL parameters and the persisted target are projections of this state, not
    competing sources of connection truth.
    """

    account: str
    project_id: str = ""
    selected_target: DeploymentTarget | None = None
    provisioned_target: DeploymentTarget | None = None
    project_candidates: tuple[ProjectCandidate, ...] = ()
    project: ConnectionLevel = field(default_factory=ConnectionLevel)
    cluster: ConnectionLevel = field(default_factory=ConnectionLevel)
    verified_target: DeploymentTarget | None = None
    verified_at: datetime | None = None
    job: ConnectionJob | None = None
    persistence_error: str = ""
    persisted_lease: bool = False

    @property
    def connected_project(self) -> str | None:
        if self.project.phase is ConnectionPhase.CONNECTED:
            return self.project_id
        return None

    @property
    def connected_target(self) -> DeploymentTarget | None:
        if self.cluster.phase is ConnectionPhase.CONNECTED:
            return self.verified_target
        return None

    @property
    def action(self) -> ConnectionAction | None:
        return self.job.action if self.job is not None else None

    @property
    def working(self) -> bool:
        return self.job is not None

    def select_project(self, project_id: str) -> None:
        """Change disconnected selection and clear all derived state."""
        if project_id == self.project_id:
            return
        if self.connected_project or self.working:
            return
        self.project_id = project_id
        self.selected_target = None
        self.project = ConnectionLevel()
        self.cluster = ConnectionLevel()
        self.verified_target = None
        self.verified_at = None

    def select_target(self, target: DeploymentTarget | None) -> None:
        """Change cluster selection without changing connection status."""
        if not self.connected_project or self.connected_target or self.working:
            return
        if target is not None and target.project_id != self.project_id:
            raise ValueError("cluster selection must belong to the selected project")
        self.selected_target = target
        self.cluster = ConnectionLevel()

    def target_for_cluster(
        self, cluster: connections.ClusterInfo
    ) -> DeploymentTarget:
        namespace = (
            self.provisioned_target.namespace
            if self.provisioned_target
            and self.provisioned_target.project_id == self.project_id
            and self.provisioned_target.cluster_name == cluster.name
            and self.provisioned_target.location == cluster.location
            else "kubeagents-system"
        )
        return DeploymentTarget(
            self.project_id,
            cluster.name,
            cluster.location,
            namespace=namespace,
            source=(
                "kube-agents-host label"
                if cluster.is_kube_agents_host
                else "manual selection"
            ),
        )

    def _submit(
        self,
        executor: ThreadPoolExecutor,
        action: ConnectionAction,
        target: DeploymentTarget | None,
    ) -> None:
        if self.working:
            return
        cancel_event = Event()
        future = executor.submit(
            connections.run_connection_checks,
            self.project_id,
            expected_target=target,
            include_agent_runtime_probe=action is not ConnectionAction.PROJECT,
            include_telemetry_probes=action is not ConnectionAction.PROJECT,
            cancel_event=cancel_event,
        )
        self.job = ConnectionJob(
            action, self.project_id, target, future, cancel_event
        )

    def connect_project(self, executor: ThreadPoolExecutor) -> None:
        self.disconnect_cluster(delete_persisted=False)
        self.project = ConnectionLevel(ConnectionPhase.CONNECTING)
        self._submit(executor, ConnectionAction.PROJECT, None)

    def connect_cluster(self, executor: ThreadPoolExecutor) -> None:
        if not self.connected_project or self.selected_target is None:
            return
        self.cluster = ConnectionLevel(ConnectionPhase.CONNECTING)
        self._submit(executor, ConnectionAction.CLUSTER, self.selected_target)

    def refresh(self, executor: ThreadPoolExecutor) -> None:
        if self.connected_target is None or self.working:
            return
        self._submit(executor, ConnectionAction.REFRESH, self.connected_target)

    def resume(self, executor: ThreadPoolExecutor) -> None:
        """Revalidate a retained target that is not currently usable."""
        if self.selected_target is None or self.working:
            return
        self._submit(executor, ConnectionAction.REFRESH, self.selected_target)

    def abort(self) -> ConnectionAction | None:
        """Detach an explicit connection attempt; refresh is not user-abortable."""
        job = self.job
        if job is None or job.action is ConnectionAction.REFRESH:
            return None
        self.job = None
        job.cancel_event.set()
        job.future.cancel()
        if job.action is ConnectionAction.PROJECT:
            self.project = ConnectionLevel()
            self.cluster = ConnectionLevel()
            self.verified_target = None
            self.verified_at = None
        elif job.action is ConnectionAction.CLUSTER:
            self.cluster = ConnectionLevel()
            self.verified_target = None
            self.verified_at = None
        return job.action

    def _detach_non_project_job(self) -> None:
        """Detach cluster work so an explicit disconnect cannot be undone."""
        job = self.job
        if job is None or job.action is ConnectionAction.PROJECT:
            return
        self.job = None
        job.cancel_event.set()
        job.future.cancel()

    def disconnect_cluster(self, *, delete_persisted: bool = True) -> None:
        self._detach_non_project_job()
        self.cluster = ConnectionLevel()
        self.verified_target = None
        self.verified_at = None
        self.persistence_error = ""
        if delete_persisted:
            delete_connection()
        self.persisted_lease = False

    def disconnect_project(self) -> None:
        if self.job:
            self.abort()
        self.disconnect_cluster(delete_persisted=False)
        self.project = ConnectionLevel()
        self.selected_target = None
        delete_connection()

    @staticmethod
    def _failure(
        report: connections.ConnectionReport,
        scope: str,
        keys: tuple[str, ...],
    ) -> str:
        checks = {check.key: check for check in report.checks}
        check = next(
            (
                checks[key]
                for key in keys
                if key in checks
                and checks[key].status is not connections.CheckStatus.PASS
            ),
            next(
                (
                    item
                    for item in report.checks
                    if item.status is connections.CheckStatus.FAIL
                ),
                None,
            ),
        )
        if check is None:
            return f"Could not connect to {scope}. Retry the connection."
        summary = check.summary.strip().rstrip(".")
        guidance = check.guidance.strip().rstrip(".")
        detail = f"{check.label}: {summary}."
        if guidance and guidance.casefold() != summary.casefold():
            detail += f" {guidance}."
        return f"Could not connect to {scope}. {detail}"

    def _select_discovered_target(
        self, report: connections.ConnectionReport
    ) -> None:
        current = self.selected_target
        if current and any(
            item.name == current.cluster_name and item.location == current.location
            for item in report.clusters
        ):
            return
        self.selected_target = (
            self.target_for_cluster(report.kube_agents_hosts[0])
            if len(report.kube_agents_hosts) == 1
            else None
        )

    def _persist(self, target: DeploymentTarget, checked_at: datetime) -> None:
        try:
            save_connection(self.account, target, checked_at)
            self.persistence_error = ""
            self.persisted_lease = True
        except (OSError, ValueError) as exc:
            self.persisted_lease = False
            self.persistence_error = (
                "Connection is active for this browser session but could not be "
                f"persisted ({type(exc).__name__})."
            )

    def _require_revalidation(
        self, target: DeploymentTarget, verified_at: datetime | None
    ) -> None:
        """Retain selection while preventing API use of a failed lease."""
        try:
            save_connection(
                self.account,
                target,
                verified_at or datetime.now(timezone.utc),
                usable=False,
            )
            self.persistence_error = ""
            self.persisted_lease = True
        except (OSError, ValueError) as exc:
            try:
                delete_connection()
            except OSError:
                pass
            self.persistence_error = (
                "The connection could not be marked for revalidation "
                f"({type(exc).__name__}); its saved target was removed."
            )
            self.persisted_lease = False

    def reconcile_persisted_lease(self) -> bool:
        """Detach a stale tab after another tab changes the shared lease."""
        if not self.persisted_lease:
            return False
        target = self.connected_target or self.selected_target
        if target is None:
            return False
        expected_usable = self.connected_target is not None
        persisted = load_connection(self.account)
        if (
            persisted
            and persisted.usable is expected_usable
            and persisted.target == target
        ):
            return False
        self.disconnect_cluster(delete_persisted=False)
        self.cluster.error = (
            "The saved cluster connection changed in another portal tab. "
            "Connect again."
        )
        return True

    def poll(self) -> ConnectionEvent | None:
        """Apply one completed job; only the currently attached job can win."""
        job = self.job
        if job is None or not job.future.done():
            return None
        self.job = None
        try:
            report = job.future.result()
        except Exception as exc:
            message = (
                "Connection checks stopped unexpectedly "
                f"({type(exc).__name__}). Retry Connect."
            )
            if job.action is ConnectionAction.PROJECT:
                self.project = ConnectionLevel(error=message)
                self.cluster = ConnectionLevel()
            elif job.action is ConnectionAction.CLUSTER:
                self.cluster = ConnectionLevel(error=message)
            else:
                failed_target = job.target
                failed_verified_at = self.verified_at
                self.disconnect_cluster(delete_persisted=False)
                if failed_target is not None:
                    self._require_revalidation(
                        failed_target, failed_verified_at
                    )
                self.cluster.error = (
                    "Cluster revalidation stopped unexpectedly "
                    f"({type(exc).__name__}). Connect again."
                )
            return ConnectionEvent(job.action, "failed", message)

        if job.action is ConnectionAction.PROJECT:
            self.project.report = report
            if connections.project_connection_is_ready(report):
                self.project.phase = ConnectionPhase.CONNECTED
                self.project.error = ""
                self._select_discovered_target(report)
                return ConnectionEvent(
                    job.action, "connected", f"Connected to project {job.project_id}."
                )
            self.project = ConnectionLevel(
                report=report,
                error=self._failure(
                    report,
                    f"project {job.project_id}",
                    ("cli_auth", "project", "gke"),
                ),
            )
            self.cluster = ConnectionLevel()
            return ConnectionEvent(job.action, "failed", self.project.error)

        target = job.target
        if job.action is ConnectionAction.CLUSTER:
            self.cluster.report = report
            if target is not None and connections.connection_is_ready(report):
                self.cluster.phase = ConnectionPhase.CONNECTED
                self.cluster.error = ""
                self.verified_target = target
                self.verified_at = report.checked_at
                self.selected_target = target
                self._persist(target, report.checked_at)
                return ConnectionEvent(
                    job.action, "connected", f"Connected to cluster {target.cluster_name}."
                )
            self.cluster = ConnectionLevel(
                report=report,
                error=self._failure(
                    report,
                    f"cluster {target.cluster_name if target else 'cluster'}",
                    ("gke", "agent_runtime"),
                ),
            )
            self.verified_target = None
            self.verified_at = None
            return ConnectionEvent(job.action, "failed", self.cluster.error)

        self.project.report = report
        if target is not None and connections.connection_is_ready(report):
            self.cluster.report = report
            self.cluster.phase = ConnectionPhase.CONNECTED
            self.cluster.error = ""
            self.verified_target = target
            self.verified_at = report.checked_at
            self._persist(target, report.checked_at)
            return ConnectionEvent(job.action, "connected")
        failed_verified_at = self.verified_at
        self.disconnect_cluster(delete_persisted=False)
        if target is not None:
            self._require_revalidation(target, failed_verified_at)
        self.cluster = ConnectionLevel(
            report=report,
            error=self._failure(
                report,
                f"cluster {target.cluster_name if target else 'cluster'}",
                ("gke", "agent_runtime"),
            ),
        )
        return ConnectionEvent(job.action, "failed", self.cluster.error)

    def refresh_due(self, now: datetime | None = None) -> bool:
        if self.connected_target is None or self.verified_at is None or self.working:
            return False
        now = now or datetime.now(timezone.utc)
        return now - self.verified_at >= CONNECTION_REFRESH_INTERVAL

    @classmethod
    def restored(
        cls,
        account: str,
        target: DeploymentTarget,
        verified_at: datetime,
        *,
        usable: bool = True,
    ) -> ConnectionController:
        """Resume an account-bound disk lease pending immediate revalidation."""
        return cls(
            account=account,
            project_id=target.project_id,
            selected_target=target,
            project=ConnectionLevel(ConnectionPhase.CONNECTED),
            cluster=ConnectionLevel(
                ConnectionPhase.CONNECTED
                if usable
                else ConnectionPhase.DISCONNECTED
            ),
            verified_target=target if usable else None,
            verified_at=verified_at,
            persisted_lease=True,
        )

    @classmethod
    def verified(
        cls,
        account: str,
        target: DeploymentTarget,
        report: connections.ConnectionReport,
    ) -> ConnectionController:
        """Construct verified state for trusted adapters and functional tests."""
        return cls(
            account=account,
            project_id=target.project_id,
            selected_target=target,
            project=ConnectionLevel(ConnectionPhase.CONNECTED, report),
            cluster=ConnectionLevel(ConnectionPhase.CONNECTED, report),
            verified_target=target,
            verified_at=report.checked_at,
        )
