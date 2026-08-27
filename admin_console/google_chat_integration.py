"""Read-only Google Chat integration state derived from live resources."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from admin_console.agent_runtime import (
    AgentRuntimeError,
    AgentRuntimeProvider,
    KubectlRunner,
)
from admin_console.connections import CommandResult, CommandRunner, GcloudRunner
from admin_console.kube_access import GKEKubeAccess, KubeCommandResult
from admin_console.project_config import DeploymentTarget
from admin_console.runtime_contract import (
    CanonicalPlatformAgentMissing,
    select_canonical_platform_agent,
)
from admin_console.telemetry import redact_kubernetes_evidence

CHAT_API = "chat.googleapis.com"
PUBSUB_API = "pubsub.googleapis.com"
WORKSPACE_ADDONS_API = "gsuiteaddons.googleapis.com"
PUBLISHER_ROLE = "roles/pubsub.publisher"
SUBSCRIBER_ROLE = "roles/pubsub.subscriber"
VIEWER_ROLE = "roles/pubsub.viewer"
ACTIVITY_WINDOW_DAYS = 30
STRAY_TOPIC_LIMIT = 15

STATUS_READY = "Ready"
STATUS_NOT_RECEIVING = "Not receiving messages"
STATUS_NEEDS_ATTENTION = "Needs attention"
STATUS_INCOMPLETE = "Verification incomplete"
STATUS_DISABLED = "Disabled"


@dataclass(frozen=True)
class IntegrationEvidence:
    source: str
    command: str
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @classmethod
    def from_kube(
        cls, source: str, arguments: list[str], result: KubeCommandResult
    ) -> "IntegrationEvidence":
        return cls(
            source,
            "kubectl " + " ".join(arguments),
            result.returncode,
            result.stdout,
            result.stderr,
            result.timed_out,
        )

    @classmethod
    def from_cloud(
        cls, source: str, arguments: list[str], result: CommandResult
    ) -> "IntegrationEvidence":
        return cls(
            source,
            "gcloud " + " ".join(arguments),
            result.returncode,
            result.stdout,
            result.stderr,
            result.timed_out,
        )


@dataclass(frozen=True)
class LiveCheck:
    """One ordered milestone: its own diagnosis and, when actionable, fix steps.

    The checklist is the single source of truth — the snapshot's status,
    message, and severity are derived from the first non-passed check.
    """

    id: str
    label: str
    status: str
    detail: str
    required: bool = True
    actions: tuple[dict[str, str], ...] = ()
    verdict: str = ""
    severity: str = ""


@dataclass(frozen=True)
class CloudQuery:
    key: str
    source: str
    arguments: tuple[str, ...]


def _json_object(evidence: IntegrationEvidence) -> dict[str, Any]:
    if evidence.returncode != 0:
        return {}
    try:
        payload = json.loads(evidence.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _json_list(evidence: IntegrationEvidence) -> list[dict[str, Any]]:
    if evidence.returncode != 0:
        return []
    try:
        payload = json.loads(evidence.stdout or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _policy_bindings(evidence: IntegrationEvidence) -> set[tuple[str, str]]:
    bindings = set()
    for binding in _json_object(evidence).get("bindings", []):
        if not isinstance(binding, dict):
            continue
        if binding.get("condition"):
            continue
        role = str(binding.get("role") or "")
        for member in binding.get("members", []):
            bindings.add((role, str(member)))
    return bindings


def _publisher_members(evidence: IntegrationEvidence) -> set[str]:
    """Return publisher members regardless of conditions; hints, not grants."""
    members = set()
    for binding in _json_object(evidence).get("bindings", []):
        if not isinstance(binding, dict):
            continue
        if str(binding.get("role") or "") != PUBLISHER_ROLE:
            continue
        members.update(str(member) for member in binding.get("members", []))
    return members


def _resource_status(evidence: IntegrationEvidence) -> str:
    if evidence.returncode == 0:
        return "passed"
    output = f"{evidence.stdout}\n{evidence.stderr}".lower()
    if "not found" in output or "does not exist" in output:
        return "failed"
    return "unknown"


class GoogleChatIntegrationService:
    """Inspect canonical Google Chat wiring without changing cloud state."""

    def __init__(
        self,
        target: DeploymentTarget,
        *,
        kube: GKEKubeAccess | None = None,
        cloud: CommandRunner | None = None,
        runtime: AgentRuntimeProvider | None = None,
    ) -> None:
        self.target = target
        self.kube = kube or GKEKubeAccess(target)
        account = os.environ.get("KUBE_AGENTS_ADMIN_USER", "").strip()
        self.cloud = cloud or GcloudRunner(account)
        self.runtime = runtime or AgentRuntimeProvider(
            target,
            runner=KubectlRunner(target, access=self.kube),
        )

    @staticmethod
    def _text(value: object) -> str:
        return str(value or "").strip()

    @staticmethod
    def _ready(agent: dict[str, Any]) -> bool:
        return any(
            condition.get("type") == "Ready"
            and condition.get("status") == "True"
            for condition in (agent.get("status") or {}).get("conditions", [])
            if isinstance(condition, dict)
        )

    def _kubectl(self, source: str, arguments: list[str]) -> IntegrationEvidence:
        result = self.kube.run(arguments, timeout=30)
        return IntegrationEvidence.from_kube(source, arguments, result)

    def _gcloud(self, query: CloudQuery) -> IntegrationEvidence:
        arguments = list(query.arguments)
        result = self.cloud.run(arguments, timeout=30)
        return IntegrationEvidence.from_cloud(query.source, arguments, result)

    def _cloud_evidence(
        self,
        project_id: str,
        topic_name: str,
        subscription_name: str,
    ) -> dict[str, IntegrationEvidence]:
        project = f"--project={project_id}"
        queries = (
            CloudQuery(
                "project",
                "Google Cloud project",
                ("projects", "describe", project_id, "--format=json"),
            ),
            CloudQuery(
                "apis",
                "Enabled integration APIs",
                (
                    "services",
                    "list",
                    "--enabled",
                    project,
                    "--format=json(config.name)",
                ),
            ),
            CloudQuery(
                "topic",
                "Configured Pub/Sub topic",
                (
                    "pubsub",
                    "topics",
                    "describe",
                    topic_name,
                    project,
                    "--format=json",
                ),
            ),
            CloudQuery(
                "topic_policy",
                "Configured topic IAM policy",
                (
                    "pubsub",
                    "topics",
                    "get-iam-policy",
                    topic_name,
                    project,
                    "--format=json",
                ),
            ),
            CloudQuery(
                "subscription",
                "Configured Pub/Sub subscription",
                (
                    "pubsub",
                    "subscriptions",
                    "describe",
                    subscription_name,
                    project,
                    "--format=json",
                ),
            ),
            CloudQuery(
                "subscription_policy",
                "Configured subscription IAM policy",
                (
                    "pubsub",
                    "subscriptions",
                    "get-iam-policy",
                    subscription_name,
                    project,
                    "--format=json",
                ),
            ),
            CloudQuery(
                "topics_list",
                "Project Pub/Sub topics",
                ("pubsub", "topics", "list", project, "--format=json(name)"),
            ),
        )
        with ThreadPoolExecutor(max_workers=len(queries)) as executor:
            results = executor.map(self._gcloud, queries)
        return {query.key: result for query, result in zip(queries, results)}

    def _stray_chat_topics(
        self,
        cloud: dict[str, IntegrationEvidence],
        topic_path: str,
        project_id: str,
        chat_publishers: set[str],
    ) -> tuple[list[str], list[IntegrationEvidence]]:
        """Find other topics that accept Google Chat publishers.

        The Chat console's saved topic has no read API, so a second topic the
        Chat service accounts can publish to is the observable trace of the
        console pointing somewhere else.
        """
        others = [
            name
            for item in _json_list(cloud["topics_list"])
            if (name := self._text(item.get("name"))) and name != topic_path
        ][:STRAY_TOPIC_LIMIT]
        if not others or not chat_publishers:
            return [], []
        queries = [
            CloudQuery(
                f"stray_policy:{name}",
                f"IAM policy of {name.rsplit('/', 1)[-1]}",
                (
                    "pubsub",
                    "topics",
                    "get-iam-policy",
                    name,
                    f"--project={project_id}",
                    "--format=json",
                ),
            )
            for name in others
        ]
        with ThreadPoolExecutor(max_workers=len(queries)) as executor:
            results = list(executor.map(self._gcloud, queries))
        strays = [
            name
            for name, evidence in zip(others, results)
            if _publisher_members(evidence) & chat_publishers
        ]
        return strays, results

    @staticmethod
    def _verdict(checks: list[LiveCheck]) -> tuple[str, str, str]:
        """Derive (status, message, severity) from the first non-passed check."""
        problem = next(
            (check for check in checks if check.status != "passed"), None
        )
        if problem is None:
            activity = next(
                (check for check in checks if check.id == "recent_activity"),
                None,
            )
            detail = activity.detail if activity else ""
            return (
                STATUS_READY,
                f"Google Chat integration is ready. {detail}".strip(),
                "success",
            )
        if not problem.required:
            status = STATUS_NOT_RECEIVING
        elif problem.status == "failed":
            status = STATUS_NEEDS_ATTENTION
        else:
            status = STATUS_INCOMPLETE
        severity = "error" if problem.status == "failed" else "warning"
        return (
            problem.verdict or status,
            problem.detail,
            problem.severity or severity,
        )

    @staticmethod
    def _milestone(
        conditions: list[tuple[str, str]], passed_detail: str
    ) -> tuple[str, str]:
        """Roll sub-conditions into one milestone: the first non-passed wins."""
        for status, detail in conditions:
            if status != "passed":
                return status, detail
        return "passed", passed_detail

    def _snapshot(
        self,
        checked_at: datetime,
        *,
        checks: list[LiveCheck],
        configuration: dict[str, Any] | None = None,
        activity: dict[str, Any] | None = None,
        evidence: list[IntegrationEvidence] | None = None,
        next_steps: tuple[dict[str, str], ...] = (),
    ) -> dict[str, Any]:
        status, message, severity = self._verdict(checks)
        return {
            "target": asdict(self.target),
            "checkedAt": checked_at.isoformat(),
            "status": status,
            "message": message,
            "severity": severity,
            "configuration": configuration or {},
            "activity": activity or {},
            "checks": [asdict(item) for item in checks],
            "evidence": [asdict(item) for item in evidence or []],
            "nextSteps": [dict(item) for item in next_steps],
        }

    def _recent_activity(
        self, canonical_name: str, checked_at: datetime
    ) -> tuple[dict[str, Any], LiveCheck]:
        try:
            history = self.runtime.list_conversations(
                canonical_name,
                cutoff=checked_at - timedelta(days=ACTIVITY_WINDOW_DAYS),
                limit=500,
            )
        except AgentRuntimeError as exc:
            detail = str(exc)
            if exc.guidance:
                detail = f"{detail} {exc.guidance}"
            return (
                {
                    "windowDays": ACTIVITY_WINDOW_DAYS,
                    "sessionCount": None,
                    "latestAt": "",
                    "truncated": False,
                },
                LiveCheck(
                    "recent_activity",
                    "Google Chat message received",
                    "unknown",
                    detail,
                    required=False,
                ),
            )

        conversations = [
            item
            for item in history.conversations
            if item.platform == "google_chat"
        ]
        latest = max(
            (item.last_active for item in conversations),
            default=None,
        )
        count = len(conversations)
        if count:
            qualifier = "at least " if history.truncated else ""
            activity_status = "passed"
            detail = (
                f"Received {qualifier}{count} Google Chat conversation"
                f"{'s' if count != 1 else ''} in the last "
                f"{ACTIVITY_WINDOW_DAYS} days; the latest at "
                f"{latest.isoformat() if latest else 'an unknown time'} "
                "proves the Chat app topic as of that moment."
            )
        elif history.truncated:
            activity_status = "unknown"
            detail = (
                "No Google Chat conversation appears in the newest 500 "
                "sessions. The read was truncated, so older activity in the "
                "time window was not inspected."
            )
        else:
            activity_status = "not_observed"
            detail = (
                "No Google Chat message has reached this installation in the "
                f"last {ACTIVITY_WINDOW_DAYS} days."
            )
        return (
            {
                "windowDays": ACTIVITY_WINDOW_DAYS,
                "sessionCount": count,
                "latestAt": latest.isoformat() if latest else "",
                "truncated": history.truncated,
            },
            LiveCheck(
                "recent_activity",
                "Google Chat message received",
                activity_status,
                detail,
                required=False,
            ),
        )

    def inspect(self) -> dict[str, Any]:
        checked_at = datetime.now(UTC)
        platform_agents = self._kubectl(
            "PlatformAgent resources",
            ["get", "platformagents", "-n", self.target.namespace, "-o", "json"],
        )
        evidence = [
            replace(
                platform_agents,
                stdout=redact_kubernetes_evidence(platform_agents.stdout),
                stderr=redact_kubernetes_evidence(platform_agents.stderr),
            )
        ]
        if platform_agents.returncode != 0:
            message = (
                "Could not read PlatformAgent resources from the selected "
                "cluster."
            )
            return self._snapshot(
                checked_at,
                checks=[
                    LiveCheck(
                        "agent_running",
                        "Agent is running",
                        "unknown",
                        message,
                    )
                ],
                evidence=evidence,
            )
        payload = _json_object(platform_agents)
        raw_agents = [
            item for item in payload.get("items", []) if isinstance(item, dict)
        ]

        try:
            canonical_name = select_canonical_platform_agent(payload)
        except CanonicalPlatformAgentMissing as exc:
            return self._snapshot(
                checked_at,
                checks=[
                    LiveCheck(
                        "agent_running",
                        "Agent is running",
                        "failed",
                        str(exc),
                    )
                ],
                evidence=evidence,
            )

        agent = next(
            item
            for item in raw_agents
            if self._text((item.get("metadata") or {}).get("name"))
            == canonical_name
        )
        spec = agent.get("spec") or {}
        chat = (spec.get("integration") or {}).get("googleChat") or {}
        enabled = chat.get("enabled") is True
        project_id = self._text(chat.get("projectId"))
        topic_name = self._text(chat.get("topicName"))
        subscription_name = self._text(chat.get("subscriptionName"))
        topic_path = (
            f"projects/{project_id}/topics/{topic_name}"
            if project_id and topic_name
            else ""
        )
        subscription_path = (
            f"projects/{project_id}/subscriptions/{subscription_name}"
            if project_id and subscription_name
            else ""
        )
        annotations = (spec.get("security") or {}).get(
            "serviceAccountAnnotations", {}
        )
        agent_service_account = self._text(
            annotations.get("iam.gke.io/gcp-service-account")
            if isinstance(annotations, dict)
            else ""
        )
        allowed_users = [
            self._text(value)
            for value in chat.get("allowedUsers", [])
            if self._text(value)
        ]
        configuration = {
            "platformAgent": canonical_name,
            "enabled": enabled,
            "projectId": project_id,
            "topicName": topic_name,
            "topicPath": topic_path,
            "subscriptionName": subscription_name,
            "subscriptionPath": subscription_path,
            "mode": self._text(chat.get("mode")) or "default",
            "allowedUsers": allowed_users,
            "allowsAllUsers": not allowed_users,
            "homeChannel": self._text(chat.get("homeChannel")),
            "agentServiceAccount": agent_service_account,
            "configurationUrl": (
                "https://console.cloud.google.com/apis/api/"
                "chat.googleapis.com/hangouts-chat?project="
                + quote(project_id, safe="")
                if project_id
                else ""
            ),
        }
        ready = self._ready(agent)
        checks = [
            LiveCheck(
                "agent_running",
                "Agent is running",
                "passed" if ready else "failed",
                (
                    f"PlatformAgent/{canonical_name} is running and Ready."
                    if ready
                    else f"PlatformAgent/{canonical_name} is not Ready."
                ),
            ),
        ]

        routing_complete = bool(project_id and topic_name and subscription_name)
        if not enabled:
            checks.append(
                LiveCheck(
                    "chat_configured",
                    "Google Chat configured on the agent",
                    "failed",
                    "Google Chat is turned off for this installation.",
                    actions=(
                        {
                            "text": (
                                "Set `spec.integration.googleChat.enabled: "
                                f"true` on `PlatformAgent/{canonical_name}` "
                                "with the project, topic, and subscription "
                                "to use, then select **Refresh status**."
                            ),
                        },
                    ),
                    verdict=STATUS_DISABLED,
                    severity="info",
                )
            )
        else:
            checks.append(
                LiveCheck(
                    "chat_configured",
                    "Google Chat configured on the agent",
                    "passed" if routing_complete else "failed",
                    (
                        "Google Chat is turned on and declares its project, "
                        "topic, and subscription."
                        if routing_complete
                        else (
                            "The enabled PlatformAgent must declare "
                            "projectId, topicName, and subscriptionName."
                        )
                    ),
                )
            )
        if not enabled or not routing_complete:
            return self._snapshot(
                checked_at,
                configuration=configuration,
                checks=checks,
                evidence=evidence,
            )

        cloud = self._cloud_evidence(project_id, topic_name, subscription_name)
        evidence.extend(cloud.values())
        project = _json_object(cloud["project"])
        project_number = self._text(project.get("projectNumber"))
        enabled_apis = {
            self._text((item.get("config") or {}).get("name"))
            for item in _json_list(cloud["apis"])
        }
        topic_status = _resource_status(cloud["topic"])
        subscription_status = _resource_status(cloud["subscription"])
        subscription = _json_object(cloud["subscription"])
        actual_topic = self._text(subscription.get("topic"))
        subscription_state = self._text(subscription.get("state"))
        alternate_delivery = any(
            bool(subscription.get(field))
            for field in (
                "pushConfig",
                "bigqueryConfig",
                "cloudStorageConfig",
                "bigtableConfig",
            )
        )
        subscription_detached = subscription.get("detached") is True
        topic_bindings = _policy_bindings(cloud["topic_policy"])
        subscription_bindings = _policy_bindings(cloud["subscription_policy"])
        system_publisher = "serviceAccount:chat-api-push@system.gserviceaccount.com"
        workspace_publisher = (
            "serviceAccount:service-"
            f"{project_number}@gcp-sa-gsuiteaddons.iam.gserviceaccount.com"
            if project_number
            else ""
        )
        subscriber = (
            f"serviceAccount:{agent_service_account}"
            if agent_service_account
            else ""
        )
        required_subscriber_bindings = {
            (SUBSCRIBER_ROLE, subscriber),
            (VIEWER_ROLE, subscriber),
        }
        subscriber_present = bool(subscriber) and (
            required_subscriber_bindings.issubset(subscription_bindings)
        )
        configuration.update(
            {
                "projectNumber": project_number,
                "workspaceServiceAccount": workspace_publisher.removeprefix(
                    "serviceAccount:"
                ),
                "subscriptionState": subscription_state,
            }
        )

        def policy_status(evidence_key: str, present: bool) -> str:
            if cloud[evidence_key].returncode != 0:
                return "unknown"
            return "passed" if present else "failed"

        apis_readable = cloud["apis"].returncode == 0
        publishers_present = (
            (PUBLISHER_ROLE, system_publisher) in topic_bindings
            and bool(workspace_publisher)
            and (PUBLISHER_ROLE, workspace_publisher) in topic_bindings
        )

        missing_apis = [
            api_label
            for api, api_label in (
                (CHAT_API, "the Google Chat API"),
                (PUBSUB_API, "the Pub/Sub API"),
                (WORKSPACE_ADDONS_API, "the Google Workspace Add-ons API"),
            )
            if api not in enabled_apis
        ]
        apis_status, apis_detail = self._milestone(
            [
                (
                    "unknown" if not apis_readable else "passed",
                    "Could not read enabled services in the integration "
                    "project.",
                ),
                (
                    "failed" if missing_apis else "passed",
                    f"Enable {', '.join(missing_apis)} in the integration "
                    "project.",
                ),
            ],
            "The Google Chat, Pub/Sub, and Google Workspace Add-ons APIs "
            "are enabled.",
        )
        checks.append(
            LiveCheck(
                "apis_enabled",
                "Required Google APIs enabled",
                apis_status,
                apis_detail,
            )
        )

        topic_ready_status, topic_ready_detail = self._milestone(
            [
                (topic_status, f"Could not verify {topic_path}."),
                (
                    "unknown" if not project_number else "passed",
                    "Could not derive the Google Workspace service identity.",
                ),
                (
                    policy_status("topic_policy", publishers_present),
                    (
                        "The topic IAM policy could not be read; Google Chat "
                        "publisher access is unverified."
                        if cloud["topic_policy"].returncode != 0
                        else (
                            "The configured topic is missing an expected "
                            "unconditional Google Chat publisher binding."
                        )
                    ),
                ),
            ],
            f"{topic_path} exists and Google Chat can publish to it.",
        )
        checks.append(
            LiveCheck(
                "topic_ready",
                "Topic ready for Google Chat",
                topic_ready_status,
                topic_ready_detail,
            )
        )

        subscription_ready_status, subscription_ready_detail = self._milestone(
            [
                (subscription_status, f"Could not verify {subscription_path}."),
                (
                    "passed" if actual_topic == topic_path else "failed",
                    "The configured subscription points to a different topic.",
                ),
                (
                    "passed" if subscription_state == "ACTIVE" else "failed",
                    "The configured subscription is not active.",
                ),
                (
                    (
                        "failed"
                        if alternate_delivery or subscription_detached
                        else "passed"
                    ),
                    "The configured subscription is detached or uses "
                    "push/export delivery; the Platform Agent requires a "
                    "pull subscription.",
                ),
                (
                    policy_status("subscription_policy", subscriber_present),
                    (
                        "The subscription IAM policy could not be read; "
                        "Platform Agent access is unverified."
                        if cloud["subscription_policy"].returncode != 0
                        else (
                            f"{agent_service_account} is missing an "
                            "unconditional subscriber or viewer binding on "
                            "the subscription."
                            if agent_service_account
                            else (
                                "The PlatformAgent does not declare its "
                                "Google service account."
                            )
                        )
                    ),
                ),
            ],
            f"{subscription_path} is active, uses pull delivery, and "
            f"{agent_service_account} can read it.",
        )
        checks.append(
            LiveCheck(
                "subscription_ready",
                "Subscription ready for the agent",
                subscription_ready_status,
                subscription_ready_detail,
            )
        )
        activity, activity_check = self._recent_activity(
            canonical_name, checked_at
        )
        session_count = activity.get("sessionCount")
        delivering = isinstance(session_count, int) and session_count > 0
        # Only a complete, empty read proves silence; a failed or truncated
        # read must not be reported as "no message arrived".
        observed_empty = session_count == 0 and not activity.get("truncated")
        stray_topics: list[str] = []
        if observed_empty:
            chat_publishers = {system_publisher}
            if workspace_publisher:
                chat_publishers.add(workspace_publisher)
            stray_topics, stray_evidence = self._stray_chat_topics(
                cloud, topic_path, project_id, chat_publishers
            )
            evidence.extend(stray_evidence)
        configuration["strayChatTopics"] = stray_topics
        stray_names = ", ".join(
            name.rsplit("/", 1)[-1] for name in stray_topics
        )
        console_actions = (
            {
                "text": (
                    "Test on Google Chat by installing the app: in "
                    "[Google Chat](https://chat.google.com), select "
                    "**New chat**, search for the app by its App name, and "
                    "send it a direct message, then select **Refresh "
                    "status**. If the app does not appear, add yourself "
                    "under its visibility settings on the configuration "
                    "page below. If you hit any errors, verify the "
                    "connection type, topic, and subscription below."
                ),
            },
            {
                "text": (
                    "Chat app configuration page — paste this link into a "
                    "browser signed in to a Google account with access to "
                    f"`{project_id}`, and confirm the app status is "
                    "**LIVE**:"
                ),
                "copy": configuration["configurationUrl"],
            },
            {
                "text": (
                    "**Connection settings** must use **Cloud Pub/Sub** "
                    "with exactly this topic:"
                ),
                "copy": topic_path,
            },
            {
                "text": (
                    "The agent reads this subscription; it must exist and "
                    "stay attached to the topic above:"
                ),
                "copy": subscription_path,
            },
        )
        if delivering:
            console_check = LiveCheck(
                "chat_console_topic",
                "Chat app sends to this installation",
                "passed",
                "Google Chat messages reach this installation, so the Chat "
                "app was pointed at the configured topic as of the last "
                "received message.",
                required=False,
            )
        elif not observed_empty:
            console_check = LiveCheck(
                "chat_console_topic",
                "Chat app sends to this installation",
                "unknown",
                "Could not determine whether Google Chat messages arrive: "
                "the recent-activity read failed or was truncated, and the "
                "Chat app setting cannot be read by API.",
                required=False,
                actions=console_actions,
                verdict=STATUS_INCOMPLETE,
            )
        elif stray_topics:
            console_check = LiveCheck(
                "chat_console_topic",
                "Chat app sends to this installation",
                "failed",
                "No Google Chat message has arrived. If the app has been "
                "messaged already, it is most likely still sending to an "
                f"old topic — {stray_names} also accept Google Chat "
                "messages, but nothing here reads them. It must send to "
                f"{topic_path}.",
                required=False,
                actions=console_actions,
            )
        else:
            console_check = LiveCheck(
                "chat_console_topic",
                "Chat app sends to this installation",
                "unknown",
                "Google Chat integration is ready — no message has been "
                "received yet. Install the Chat app and send it a message "
                "to test.",
                required=False,
                actions=console_actions,
                verdict=STATUS_READY,
                severity="info",
            )
        checks.append(console_check)
        checks.append(activity_check)

        all_passed = all(check.status == "passed" for check in checks)
        return self._snapshot(
            checked_at,
            configuration=configuration,
            activity=activity,
            checks=checks,
            evidence=evidence,
            next_steps=console_actions if all_passed else (),
        )
