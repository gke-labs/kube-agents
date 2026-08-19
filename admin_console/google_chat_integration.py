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
    id: str
    label: str
    status: str
    detail: str
    required: bool = True


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
        )
        with ThreadPoolExecutor(max_workers=len(queries)) as executor:
            results = executor.map(self._gcloud, queries)
        return {query.key: result for query, result in zip(queries, results)}

    @staticmethod
    def _summary(checks: list[LiveCheck], *, enabled: bool) -> tuple[str, str]:
        if not enabled:
            return "Disabled", "Google Chat is disabled on the canonical PlatformAgent."
        failed = next(
            (
                check
                for check in checks
                if check.required and check.status == "failed"
            ),
            None,
        )
        if failed:
            return "Needs attention", failed.detail
        unknown = next(
            (
                check
                for check in checks
                if check.required and check.status == "unknown"
            ),
            None,
        )
        if unknown:
            return "Verification incomplete", unknown.detail
        return (
            "Backend ready",
            "The canonical PlatformAgent and every verifiable Pub/Sub backend "
            "check passed.",
        )

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
                    "Recent Google Chat activity",
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
            detail = (
                f"Observed {qualifier}{count} Google Chat session"
                f"{'s' if count != 1 else ''} in the last "
                f"{ACTIVITY_WINDOW_DAYS} days."
            )
        elif history.truncated:
            detail = (
                "No Google Chat session appears in the newest 500 sessions. "
                "The read was truncated, so older activity in the time window "
                "was not inspected."
            )
        else:
            detail = (
                "No Google Chat session has reached this installation in the "
                f"last {ACTIVITY_WINDOW_DAYS} days."
            )
        if count:
            activity_status = "passed"
        elif history.truncated:
            activity_status = "unknown"
        else:
            activity_status = "not_observed"
        return (
            {
                "windowDays": ACTIVITY_WINDOW_DAYS,
                "sessionCount": count,
                "latestAt": latest.isoformat() if latest else "",
                "truncated": history.truncated,
            },
            LiveCheck(
                "recent_activity",
                "Recent Google Chat activity",
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
            return {
                "target": asdict(self.target),
                "checkedAt": checked_at.isoformat(),
                "status": "Verification incomplete",
                "message": (
                    "Could not read PlatformAgent resources from the selected "
                    "cluster."
                ),
                "configuration": {},
                "activity": {},
                "checks": [
                    asdict(
                        LiveCheck(
                            "canonical_agent",
                            "Canonical PlatformAgent",
                            "unknown",
                            "Could not read PlatformAgent resources from the "
                            "selected cluster.",
                        )
                    )
                ],
                "evidence": [asdict(item) for item in evidence],
            }
        payload = _json_object(platform_agents)
        raw_agents = [
            item for item in payload.get("items", []) if isinstance(item, dict)
        ]

        try:
            canonical_name = select_canonical_platform_agent(payload)
        except CanonicalPlatformAgentMissing as exc:
            return {
                "target": asdict(self.target),
                "checkedAt": checked_at.isoformat(),
                "status": "Needs attention",
                "message": str(exc),
                "configuration": {},
                "activity": {},
                "checks": [
                    asdict(
                        LiveCheck(
                            "canonical_agent",
                            "Canonical PlatformAgent",
                            "failed",
                            str(exc),
                        )
                    )
                ],
                "evidence": [asdict(item) for item in evidence],
            }

        agent = next(
            item
            for item in raw_agents
            if self._text((item.get("metadata") or {}).get("name"))
            == canonical_name
        )
        spec = agent.get("spec") or {}
        chat = ((spec.get("integration") or {}).get("googleChat") or {})
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
        checks = [
            LiveCheck(
                "canonical_agent",
                "Canonical PlatformAgent",
                "passed",
                f"PlatformAgent/{canonical_name} was discovered.",
            ),
            LiveCheck(
                "agent_ready",
                "PlatformAgent ready",
                "passed" if self._ready(agent) else "failed",
                (
                    f"PlatformAgent/{canonical_name} reports Ready=True."
                    if self._ready(agent)
                    else f"PlatformAgent/{canonical_name} is not Ready."
                ),
            ),
        ]
        if not enabled:
            status, message = self._summary(checks, enabled=False)
            return {
                "target": asdict(self.target),
                "checkedAt": checked_at.isoformat(),
                "status": status,
                "message": message,
                "configuration": configuration,
                "activity": {},
                "checks": [asdict(item) for item in checks],
                "evidence": [asdict(item) for item in evidence],
            }

        routing_complete = bool(project_id and topic_name and subscription_name)
        checks.append(
            LiveCheck(
                "routing_configured",
                "Pub/Sub route configured",
                "passed" if routing_complete else "failed",
                (
                    "The PlatformAgent declares a project, topic, and subscription."
                    if routing_complete
                    else (
                        "The enabled PlatformAgent must declare projectId, "
                        "topicName, and subscriptionName."
                    )
                ),
            )
        )
        if not routing_complete:
            status, message = self._summary(checks, enabled=True)
            return {
                "target": asdict(self.target),
                "checkedAt": checked_at.isoformat(),
                "status": status,
                "message": message,
                "configuration": configuration,
                "activity": {},
                "checks": [asdict(item) for item in checks],
                "evidence": [asdict(item) for item in evidence],
            }

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

        checks.extend(
            (
                LiveCheck(
                    "chat_api",
                    "Google Chat API enabled",
                    (
                        "unknown"
                        if not apis_readable
                        else "passed" if CHAT_API in enabled_apis else "failed"
                    ),
                    (
                        "Google Chat API is enabled."
                        if CHAT_API in enabled_apis
                        else (
                            "Enable the Google Chat API in the integration project."
                            if apis_readable
                            else (
                                "Could not read enabled services in the "
                                "integration project."
                            )
                        )
                    ),
                ),
                LiveCheck(
                    "pubsub_api",
                    "Pub/Sub API enabled",
                    (
                        "unknown"
                        if not apis_readable
                        else "passed" if PUBSUB_API in enabled_apis else "failed"
                    ),
                    (
                        "Pub/Sub API is enabled."
                        if PUBSUB_API in enabled_apis
                        else (
                            "Enable the Pub/Sub API in the integration project."
                            if apis_readable
                            else (
                                "Could not read enabled services in the "
                                "integration project."
                            )
                        )
                    ),
                ),
                LiveCheck(
                    "workspace_addons_api",
                    "Google Workspace Add-ons API enabled",
                    (
                        "unknown"
                        if not apis_readable
                        else (
                            "passed"
                            if WORKSPACE_ADDONS_API in enabled_apis
                            else "failed"
                        )
                    ),
                    (
                        "Google Workspace Add-ons API is enabled."
                        if WORKSPACE_ADDONS_API in enabled_apis
                        else (
                            "Enable the Google Workspace Add-ons API in the "
                            "integration project."
                            if apis_readable
                            else (
                                "Could not read enabled services in the "
                                "integration project."
                            )
                        )
                    ),
                ),
                LiveCheck(
                    "topic_exists",
                    "Configured topic exists",
                    topic_status,
                    (
                        f"{topic_path} exists."
                        if topic_status == "passed"
                        else f"Could not verify {topic_path}."
                    ),
                ),
                LiveCheck(
                    "chat_publishers",
                    "Publisher IAM bindings present",
                    (
                        "unknown"
                        if not project_number
                        else policy_status("topic_policy", publishers_present)
                    ),
                    (
                        "Both expected Google Chat publisher bindings are "
                        "present without conditions on the configured topic."
                        if publishers_present
                        else (
                            "Could not derive the Google Workspace service identity."
                            if not project_number
                            else (
                                "The configured topic is missing an expected "
                                "unconditional Google Chat publisher binding."
                            )
                        )
                    ),
                ),
                LiveCheck(
                    "subscription_exists",
                    "Configured subscription exists",
                    subscription_status,
                    (
                        f"{subscription_path} exists."
                        if subscription_status == "passed"
                        else f"Could not verify {subscription_path}."
                    ),
                ),
                LiveCheck(
                    "subscription_topic",
                    "Subscription uses configured topic",
                    (
                        "unknown"
                        if subscription_status == "unknown"
                        else "passed" if actual_topic == topic_path else "failed"
                    ),
                    (
                        f"The subscription reads {topic_path}."
                        if actual_topic == topic_path
                        else "The configured subscription points to a different topic."
                    ),
                ),
                LiveCheck(
                    "subscription_active",
                    "Subscription active",
                    (
                        "unknown"
                        if subscription_status == "unknown"
                        else (
                            "passed"
                            if subscription_state == "ACTIVE"
                            else "failed"
                        )
                    ),
                    (
                        "The configured subscription reports ACTIVE."
                        if subscription_state == "ACTIVE"
                        else "The configured subscription is not active."
                    ),
                ),
                LiveCheck(
                    "subscription_delivery_type",
                    "Subscription supports pull delivery",
                    (
                        "unknown"
                        if subscription_status != "passed"
                        else (
                            "passed"
                            if not alternate_delivery
                            and not subscription_detached
                            else "failed"
                        )
                    ),
                    (
                        "The subscription is attached and uses pull delivery."
                        if not alternate_delivery and not subscription_detached
                        else (
                            "The configured subscription is detached or uses "
                            "push/export delivery; the Platform Agent requires "
                            "a pull subscription."
                        )
                    ),
                ),
                LiveCheck(
                    "agent_subscriber",
                    "Platform Agent can consume",
                    policy_status("subscription_policy", subscriber_present),
                    (
                        f"{agent_service_account} can consume and inspect the "
                        "subscription through unconditional bindings."
                        if subscriber_present
                        else (
                            "The subscription IAM policy could not be read; "
                            "Platform Agent access is unverified."
                            if cloud["subscription_policy"].returncode != 0
                            else (
                                f"{agent_service_account} is missing an "
                                "unconditional subscriber or viewer binding on the "
                                "subscription."
                                if agent_service_account
                                else (
                                    "The PlatformAgent does not declare its "
                                    "Google service account."
                                )
                            )
                        )
                    ),
                ),
            )
        )
        activity, activity_check = self._recent_activity(
            canonical_name, checked_at
        )
        checks.append(activity_check)
        status, message = self._summary(checks, enabled=True)
        return {
            "target": asdict(self.target),
            "checkedAt": checked_at.isoformat(),
            "status": status,
            "message": message,
            "configuration": configuration,
            "activity": activity,
            "checks": [asdict(item) for item in checks],
            "evidence": [asdict(item) for item in evidence],
        }
