"""Read-only connectivity diagnostics for a selected Google Cloud project."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from threading import Event
from typing import Protocol

from admin_console.project_config import DeploymentTarget, is_valid_project_id

REQUIRED_APIS = (
    "container.googleapis.com",
    "logging.googleapis.com",
    "cloudtrace.googleapis.com",
)


class CheckStatus(StrEnum):
    PASS = "pass"
    WARNING = "warning"
    FAIL = "fail"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


class CommandRunner(Protocol):
    def run(self, arguments: list[str], *, timeout: int = 15) -> CommandResult: ...


class ConnectionChecksCancelled(RuntimeError):
    """Stop a detached connection probe between bounded external calls."""


class CancelAwareRunner:
    """Stop submitting commands after the UI detaches a connection attempt."""

    def __init__(self, runner: CommandRunner, cancelled: Event) -> None:
        self.runner = runner
        self.cancelled = cancelled

    def run(self, arguments: list[str], *, timeout: int = 15) -> CommandResult:
        if self.cancelled.is_set():
            raise ConnectionChecksCancelled()
        result = self.runner.run(arguments, timeout=timeout)
        if self.cancelled.is_set():
            raise ConnectionChecksCancelled()
        return result


class GcloudRunner:
    """Run non-interactive gcloud commands without a shell."""

    def __init__(self, account: str = "") -> None:
        self.account = account

    def run(self, arguments: list[str], *, timeout: int = 15) -> CommandResult:
        environment = os.environ.copy()
        environment["CLOUDSDK_CORE_DISABLE_PROMPTS"] = "1"
        command = ["gcloud", "--quiet"]
        if self.account:
            command.append(f"--account={self.account}")
        command.extend(arguments)
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=environment,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(124, timed_out=True)
        except OSError as exc:
            return CommandResult(127, stderr=type(exc).__name__)
        return CommandResult(
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )


@dataclass(frozen=True)
class ClusterInfo:
    name: str
    location: str
    status: str
    is_kube_agents_host: bool = False


@dataclass(frozen=True)
class ConnectionCheck:
    key: str
    label: str
    status: CheckStatus
    summary: str
    guidance: str = ""


@dataclass(frozen=True)
class ConnectionReport:
    project_id: str
    checked_at: datetime
    checks: tuple[ConnectionCheck, ...]
    clusters: tuple[ClusterInfo, ...] = ()

    @property
    def passed(self) -> int:
        return sum(check.status == CheckStatus.PASS for check in self.checks)

    @property
    def warnings(self) -> int:
        return sum(check.status == CheckStatus.WARNING for check in self.checks)

    @property
    def failed(self) -> int:
        return sum(check.status == CheckStatus.FAIL for check in self.checks)

    @property
    def kube_agents_hosts(self) -> tuple[ClusterInfo, ...]:
        return tuple(
            cluster for cluster in self.clusters if cluster.is_kube_agents_host
        )


def connection_is_ready(report: ConnectionReport) -> bool:
    """Return whether the selected cluster runtime is reachable.

    Telemetry checks remain visible diagnostics, but a Logging or Trace outage
    must not lock runtime-backed Chat, Kanban, or Cron pages.
    """
    checks = {check.key: check.status for check in report.checks}
    required = {"cli_auth", "project", "gke", "agent_runtime"}
    return all(checks.get(key) == CheckStatus.PASS for key in required)


def project_connection_is_ready(report: ConnectionReport) -> bool:
    """Return whether project access and GKE discovery are usable."""
    checks = {check.key: check.status for check in report.checks}
    return (
        checks.get("cli_auth") == CheckStatus.PASS
        and checks.get("project") == CheckStatus.PASS
        and checks.get("gke") in {CheckStatus.PASS, CheckStatus.WARNING}
    )


def _failure_guidance(component: str, result: CommandResult) -> tuple[str, str]:
    if result.timed_out:
        return (
            f"{component} timed out.",
            "Check network access to Google APIs and retry.",
        )
    error = result.stderr.lower()
    if "invalid_grant" in error or "reauth" in error or "login" in error:
        return (
            f"{component} authentication failed.",
            "Refresh credentials with `gcloud auth login` and "
            "`gcloud auth application-default login`.",
        )
    if "disabled" in error or "has not been used" in error:
        return (
            f"{component} API is disabled.",
            "Enable the named API in the selected project, then retry.",
        )
    if "permission" in error or "forbidden" in error or "403" in error:
        return (
            f"{component} access was denied.",
            "Ask a project administrator for the minimum read permissions shown "
            "by the failing Google API. Do not grant the portal write roles.",
        )
    return (
        f"{component} could not be reached.",
        "Confirm the selected project, gcloud configuration, and network access.",
    )


def _command_check(
    *,
    key: str,
    label: str,
    component: str,
    result: CommandResult,
    success: str,
) -> ConnectionCheck:
    if result.returncode == 0:
        return ConnectionCheck(key, label, CheckStatus.PASS, success)
    summary, guidance = _failure_guidance(component, result)
    return ConnectionCheck(key, label, CheckStatus.FAIL, summary, guidance)


def _read_json_list(result: CommandResult) -> list[dict]:
    if result.returncode != 0:
        return []
    try:
        value = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def _trace_probe(
    project_id: str,
    runner: CommandRunner,
    *,
    timeout: int = 10,
) -> ConnectionCheck:
    # Cloud Trace's REST endpoint does not accept every credential type that
    # gcloud itself can broker (for example, some enterprise CLI credentials).
    # Use ADC here and report it as a separate credential boundary in the UI.
    token_result = runner.run(
        ["auth", "application-default", "print-access-token"], timeout=timeout
    )
    if token_result.returncode != 0 or not token_result.stdout.strip():
        summary, guidance = _failure_guidance("Application Default Credentials", token_result)
        if token_result.returncode == 0:
            summary = "Application Default Credentials returned no token."
            guidance = "Run `gcloud auth application-default login`."
        return ConnectionCheck(
            "trace",
            "Cloud Trace read",
            CheckStatus.SKIPPED,
            summary,
            guidance,
        )

    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=24)
    query = urllib.parse.urlencode(
        {
            "startTime": start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": end_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "pageSize": 1,
        }
    )
    url = (
        f"https://cloudtrace.googleapis.com/v1/projects/{project_id}/traces?{query}"
    )
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token_result.stdout.strip()}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read(4096).decode("utf-8", errors="replace")
        result = CommandResult(exc.code, stderr=f"HTTP {exc.code} {error_body}")
        summary, guidance = _failure_guidance("Cloud Trace", result)
        return ConnectionCheck(
            "trace", "Cloud Trace read", CheckStatus.FAIL, summary, guidance
        )
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return ConnectionCheck(
            "trace",
            "Cloud Trace read",
            CheckStatus.FAIL,
            "Cloud Trace could not be reached.",
            "Check network access, ADC, and whether Cloud Trace is enabled.",
        )
    finally:
        # Do not retain the bearer token in report or Streamlit state.
        token_result = CommandResult(0)

    traces = payload.get("traces", []) if isinstance(payload, dict) else []
    if traces:
        return ConnectionCheck(
            "trace",
            "Cloud Trace read",
            CheckStatus.PASS,
            "Cloud Trace is readable and recent traces were found.",
        )
    return ConnectionCheck(
        "trace",
        "Cloud Trace read",
        CheckStatus.WARNING,
        "Cloud Trace is readable, but no traces were found in the last 24 hours.",
        "Confirm the agent was active and the managed OTel export is healthy.",
    )


def run_connection_checks(
    project_id: str,
    *,
    expected_target: DeploymentTarget | None = None,
    runner: CommandRunner | None = None,
    include_trace_probe: bool = True,
    include_agent_runtime_probe: bool = False,
    include_telemetry_probes: bool = True,
    cancel_event: Event | None = None,
) -> ConnectionReport:
    """Run bounded, read-only diagnostics for one selected project."""
    if not is_valid_project_id(project_id):
        raise ValueError("invalid Google Cloud project ID")
    account = os.environ.get("KUBE_AGENTS_ADMIN_USER", "")
    runner = runner or GcloudRunner(account)
    if cancel_event is not None:
        runner = CancelAwareRunner(runner, cancel_event)
    checks: list[ConnectionCheck] = []

    cli_result = runner.run(["auth", "print-access-token"])
    checks.append(
        _command_check(
            key="cli_auth",
            label="gcloud CLI identity",
            component="gcloud CLI",
            result=cli_result,
            success=f"Authenticated as {account}." if account else "gcloud is authenticated.",
        )
    )

    adc_result = runner.run(
        ["auth", "application-default", "print-access-token"]
    )
    if adc_result.returncode == 0:
        checks.append(
            ConnectionCheck(
                "adc",
                "Application Default Credentials",
                CheckStatus.PASS,
                "ADC is ready for a future native Google Cloud provider.",
            )
        )
    else:
        summary, _ = _failure_guidance("Application Default Credentials", adc_result)
        checks.append(
            ConnectionCheck(
                "adc",
                "Application Default Credentials",
                CheckStatus.WARNING,
                summary + " Live diagnostics still use the verified CLI identity.",
                "Run `gcloud auth application-default login` before enabling the "
                "native telemetry provider.",
            )
        )

    project_result = runner.run(
        ["projects", "describe", project_id, "--format=value(projectId)"]
    )
    checks.append(
        _command_check(
            key="project",
            label="Project access",
            component="Google Cloud project",
            result=project_result,
            success=f"Project {project_id} is accessible.",
        )
    )

    if project_result.returncode != 0:
        for key, label in (
            ("apis", "Required APIs"),
            ("gke", "GKE discovery"),
            ("host_discovery", "kube-agents host discovery"),
            ("logging", "Cloud Logging read"),
            ("audit", "Structured audit events"),
            ("trace", "Cloud Trace read"),
        ):
            checks.append(
                ConnectionCheck(
                    key,
                    label,
                    CheckStatus.SKIPPED,
                    "Skipped because project access failed.",
                )
            )
        return ConnectionReport(
            project_id, datetime.now(timezone.utc), tuple(checks)
        )

    api_result = runner.run(
        [
            "services",
            "list",
            "--enabled",
            f"--project={project_id}",
            "--format=value(config.name)",
        ]
    )
    if api_result.returncode == 0:
        enabled = set(api_result.stdout.splitlines())
        missing = [api for api in REQUIRED_APIS if api not in enabled]
        if missing:
            checks.append(
                ConnectionCheck(
                    "apis",
                    "Required APIs",
                    CheckStatus.WARNING,
                    "Some read APIs are not reported as enabled: "
                    + ", ".join(missing),
                    "Enable only the APIs required by the desired portal data source.",
                )
            )
        else:
            checks.append(
                ConnectionCheck(
                    "apis",
                    "Required APIs",
                    CheckStatus.PASS,
                    "GKE, Cloud Logging, and Cloud Trace APIs are enabled.",
                )
            )
    else:
        summary, guidance = _failure_guidance("Service Usage", api_result)
        checks.append(
            ConnectionCheck(
                "apis",
                "Required APIs",
                CheckStatus.WARNING,
                summary,
                guidance
                + " Direct source probes below still determine actual readability.",
            )
        )

    cluster_result = runner.run(
        [
            "container",
            "clusters",
            "list",
            f"--project={project_id}",
            "--format=json(name,location,status,resourceLabels)",
        ],
        timeout=20,
    )
    clusters = tuple(
        ClusterInfo(
            str(item.get("name", "")),
            str(item.get("location", "")),
            str(item.get("status", "")),
            str(
                (item.get("resourceLabels") or {}).get("kube-agents-host", "")
            ).lower()
            == "true",
        )
        for item in _read_json_list(cluster_result)
        if item.get("name")
    )
    if cluster_result.returncode != 0:
        summary, guidance = _failure_guidance("GKE", cluster_result)
        checks.append(
            ConnectionCheck("gke", "GKE discovery", CheckStatus.FAIL, summary, guidance)
        )
    elif not clusters:
        checks.append(
            ConnectionCheck(
                "gke",
                "GKE discovery",
                CheckStatus.WARNING,
                "GKE is readable, but no clusters were found.",
                "Confirm this is the project used by provision.sh.",
            )
        )
    else:
        expected = expected_target.cluster_name if expected_target else ""
        expected_found = not expected_target or any(
            item.name == expected_target.cluster_name
            and item.location == expected_target.location
            for item in clusters
        )
        status = CheckStatus.PASS if expected_found else CheckStatus.WARNING
        summary = f"Found {len(clusters)} GKE cluster(s)."
        guidance = ""
        if not expected_found:
            summary += f" Expected cluster {expected} was not found."
            guidance = "Check the provisioned project, region, and cluster state."
        checks.append(
            ConnectionCheck("gke", "GKE discovery", status, summary, guidance)
        )

    host_clusters = tuple(
        cluster for cluster in clusters if cluster.is_kube_agents_host
    )
    if cluster_result.returncode != 0:
        checks.append(
            ConnectionCheck(
                "host_discovery",
                "kube-agents host discovery",
                CheckStatus.SKIPPED,
                "Skipped because GKE discovery failed.",
            )
        )
    elif expected_target is not None:
        selected_is_labeled = any(
            cluster.name == expected_target.cluster_name
            and cluster.location == expected_target.location
            and cluster.is_kube_agents_host
            for cluster in clusters
        )
        checks.append(
            ConnectionCheck(
                "host_discovery",
                "kube-agents host discovery",
                CheckStatus.PASS if selected_is_labeled else CheckStatus.WARNING,
                (
                    f"Selected labeled host {expected_target.cluster_name}."
                    if selected_is_labeled
                    else (
                        "Using manually selected cluster "
                        f"{expected_target.cluster_name}."
                    )
                ),
                (
                    "The kube-agents-host=true label was not found on the selected "
                    "cluster. Please confirm the latest version of kube-agents is deployed."
                    if not selected_is_labeled
                    else ""
                ),
            )
        )
    elif len(host_clusters) == 1:
        host = host_clusters[0]
        checks.append(
            ConnectionCheck(
                "host_discovery",
                "kube-agents host discovery",
                CheckStatus.PASS,
                f"Detected {host.name} · {host.location} from kube-agents-host=true.",
            )
        )
    elif not host_clusters:
        checks.append(
            ConnectionCheck(
                "host_discovery",
                "kube-agents host discovery",
                CheckStatus.WARNING,
                "No cluster has the kube-agents-host=true label.",
                "Select the cluster that hosts kube-agents manually.",
            )
        )
    else:
        checks.append(
            ConnectionCheck(
                "host_discovery",
                "kube-agents host discovery",
                CheckStatus.WARNING,
                f"Found {len(host_clusters)} clusters labeled kube-agents-host=true.",
                "Select the intended kube-agents host manually.",
            )
        )

    if include_agent_runtime_probe:
        runtime_target = None
        if expected_target and any(
            cluster.name == expected_target.cluster_name
            and cluster.location == expected_target.location
            for cluster in clusters
        ):
            runtime_target = expected_target
        elif len(host_clusters) == 1:
            cluster = host_clusters[0]
            runtime_target = DeploymentTarget(
                project_id,
                cluster.name,
                cluster.location,
                namespace=(expected_target.namespace if expected_target else "kubeagents-system"),
            )
        if runtime_target is None:
            checks.append(
                ConnectionCheck(
                    "agent_runtime",
                    "Agent runtime read",
                    CheckStatus.SKIPPED,
                    "Select one cluster before testing persisted sessions.",
                )
            )
        else:
            from admin_console.agent_runtime import (
                AgentRuntimeError,
                AgentRuntimeProvider,
            )

            try:
                if cancel_event is not None and cancel_event.is_set():
                    raise ConnectionChecksCancelled()
                runtime_provider = AgentRuntimeProvider(runtime_target)
                agents = runtime_provider.list_agents()
                if not agents:
                    checks.append(
                        ConnectionCheck(
                            "agent_runtime",
                            "Agent runtime read",
                            CheckStatus.WARNING,
                            "No running kube-agents gateway pods were found.",
                            "Confirm the selected cluster and namespace.",
                        )
                    )
                else:
                    profile_count, session_count = runtime_provider.check_connection(
                        agents[0]
                    )
                    checks.append(
                        ConnectionCheck(
                            "agent_runtime",
                            "Agent runtime read",
                            CheckStatus.PASS,
                            f"Read {profile_count} profile(s) and {session_count} "
                            f"session(s) from {agents[0]}.",
                        )
                    )
                if cancel_event is not None and cancel_event.is_set():
                    raise ConnectionChecksCancelled()
            except AgentRuntimeError as exc:
                checks.append(
                    ConnectionCheck(
                        "agent_runtime",
                        "Agent runtime read",
                        CheckStatus.FAIL,
                        str(exc),
                        exc.guidance,
                    )
                )

    if not include_telemetry_probes:
        return ConnectionReport(
            project_id,
            datetime.now(timezone.utc),
            tuple(checks),
            clusters,
        )

    namespace = (
        expected_target.namespace
        if expected_target and expected_target.project_id == project_id
        else "kubeagents-system"
    )
    base_filter = (
        'resource.type="k8s_container" '
        f'AND resource.labels.namespace_name="{namespace}"'
    )
    log_result = runner.run(
        [
            "logging",
            "read",
            base_filter,
            f"--project={project_id}",
            "--freshness=24h",
            "--limit=1",
            "--format=json",
        ],
        timeout=20,
    )
    log_rows = _read_json_list(log_result)
    if log_result.returncode != 0:
        summary, guidance = _failure_guidance("Cloud Logging", log_result)
        checks.append(
            ConnectionCheck(
                "logging", "Cloud Logging read", CheckStatus.FAIL, summary, guidance
            )
        )
    elif log_rows:
        checks.append(
            ConnectionCheck(
                "logging",
                "Cloud Logging read",
                CheckStatus.PASS,
                f"Recent logs were found for namespace {namespace}.",
            )
        )
    else:
        checks.append(
            ConnectionCheck(
                "logging",
                "Cloud Logging read",
                CheckStatus.WARNING,
                f"Logging is readable, but no {namespace} logs were found in 24 hours.",
                "Check the selected project, agent activity, retention, and GKE log collection.",
            )
        )

    audit_filter = (
        "("
        + base_filter
        + ") AND (jsonPayload.audit_event:* "
        + 'OR jsonPayload.log:"audit_event" '
        + 'OR textPayload:"audit_event")'
    )
    audit_result = runner.run(
        [
            "logging",
            "read",
            audit_filter,
            f"--project={project_id}",
            "--freshness=24h",
            "--limit=1",
            "--format=json",
        ],
        timeout=20,
    )
    audit_rows = _read_json_list(audit_result)
    if audit_result.returncode != 0:
        summary, guidance = _failure_guidance("structured audit logs", audit_result)
        checks.append(
            ConnectionCheck(
                "audit",
                "Structured audit events",
                CheckStatus.FAIL,
                summary,
                guidance,
            )
        )
    elif audit_rows:
        checks.append(
            ConnectionCheck(
                "audit",
                "Structured audit events",
                CheckStatus.PASS,
                "Recent structured audit events were found.",
            )
        )
    else:
        checks.append(
            ConnectionCheck(
                "audit",
                "Structured audit events",
                CheckStatus.WARNING,
                "Logging is readable, but no structured audit events were found in 24 hours.",
                "Check agent activity, audit plugin configuration, and JSON log parsing.",
            )
        )

    if include_trace_probe:
        checks.append(_trace_probe(project_id, runner))
    else:
        checks.append(
            ConnectionCheck(
                "trace",
                "Cloud Trace read",
                CheckStatus.SKIPPED,
                "Trace probe disabled for this run.",
            )
        )

    return ConnectionReport(
        project_id,
        datetime.now(timezone.utc),
        tuple(checks),
        clusters,
    )
