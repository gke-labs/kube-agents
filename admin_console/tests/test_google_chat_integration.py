from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from admin_console.api.authorization import portal_api_headers
from admin_console.api.app import create_app
from admin_console.clients.portal_api import PortalApiClient
from admin_console.connections import CommandResult
from admin_console.google_chat_integration import GoogleChatIntegrationService
from admin_console.kube_access import KubeCommandResult
from admin_console.project_config import DeploymentTarget

TARGET = DeploymentTarget(
    "chat-project-01",
    "chat-cluster-01",
    "us-east4",
    "kubeagents-system",
)
TOPIC = "events-from-live-cr"
SUBSCRIPTION = "events-from-live-cr-sub"
GSA = "agent-from-live-cr@chat-project-01.iam.gserviceaccount.com"
TOPIC_PATH = f"projects/{TARGET.project_id}/topics/{TOPIC}"
SUBSCRIPTION_PATH = (
    f"projects/{TARGET.project_id}/subscriptions/{SUBSCRIPTION}"
)


def platform_agents(
    *, name: str = "platform-agent", enabled: bool = True, with_secret: bool = False
) -> dict:
    payload = {
        "items": [
            {
                "metadata": {"name": name},
                "spec": {
                    "security": {
                        "serviceAccountAnnotations": {
                            "iam.gke.io/gcp-service-account": GSA
                        }
                    },
                    "integration": {
                        "googleChat": {
                            "enabled": enabled,
                            "projectId": TARGET.project_id,
                            "topicName": TOPIC,
                            "subscriptionName": SUBSCRIPTION,
                            "mode": "debug",
                            "allowedUsers": ["operator@example.com"],
                        }
                    },
                },
                "status": {
                    "conditions": [{"type": "Ready", "status": "True"}]
                },
            }
        ]
    }
    if with_secret:
        payload["items"][0]["spec"]["deployment"] = {
            "env": [{"name": "EXTERNAL_API_TOKEN", "value": "literal-secret"}]
        }
    return payload


class FakeKube:
    def __init__(self, payload: dict, *, returncode: int = 0) -> None:
        self.payload = payload
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def run(self, arguments: list[str], *, timeout: int = 20):
        self.calls.append(arguments)
        return KubeCommandResult(
            self.returncode,
            stdout=json.dumps(self.payload) if self.returncode == 0 else "",
            stderr="forbidden" if self.returncode else "",
        )


STRAY_TOPIC_PATH = f"projects/{TARGET.project_id}/topics/hermes-chat-events"
QUIET_TOPIC_PATH = f"projects/{TARGET.project_id}/topics/unrelated-events"


class FakeCloud:
    def __init__(
        self,
        *,
        conditional_publisher: bool = False,
        omit_subscriber: bool = False,
        push_subscription: bool = False,
        stray_topic: bool = False,
    ) -> None:
        self.conditional_publisher = conditional_publisher
        self.omit_subscriber = omit_subscriber
        self.push_subscription = push_subscription
        self.stray_topic = stray_topic
        self.calls: list[list[str]] = []

    def run(self, arguments: list[str], *, timeout: int = 15):
        self.calls.append(arguments)
        command = tuple(arguments)
        if command[:2] == ("projects", "describe"):
            payload = {"projectId": TARGET.project_id, "projectNumber": "123456"}
        elif command[:2] == ("services", "list"):
            payload = [
                {"config": {"name": "chat.googleapis.com"}},
                {"config": {"name": "pubsub.googleapis.com"}},
                {"config": {"name": "gsuiteaddons.googleapis.com"}},
            ]
        elif command[:3] == ("pubsub", "topics", "list"):
            names = [TOPIC_PATH]
            if self.stray_topic:
                names.extend([STRAY_TOPIC_PATH, QUIET_TOPIC_PATH])
            payload = [{"name": name} for name in names]
        elif command[:3] == ("pubsub", "topics", "describe"):
            payload = {"name": TOPIC_PATH}
        elif command[:3] == ("pubsub", "topics", "get-iam-policy"):
            if command[3] == STRAY_TOPIC_PATH:
                payload = {
                    "bindings": [
                        {
                            "role": "roles/pubsub.publisher",
                            "members": [
                                "serviceAccount:chat-api-push@"
                                "system.gserviceaccount.com"
                            ],
                        }
                    ]
                }
                return CommandResult(0, stdout=json.dumps(payload))
            if command[3] == QUIET_TOPIC_PATH:
                return CommandResult(0, stdout=json.dumps({"bindings": []}))
            members = [
                "serviceAccount:chat-api-push@system.gserviceaccount.com"
            ]
            members.append(
                "serviceAccount:service-123456@"
                "gcp-sa-gsuiteaddons.iam.gserviceaccount.com"
            )
            binding = {"role": "roles/pubsub.publisher", "members": members}
            if self.conditional_publisher:
                binding["condition"] = {
                    "title": "expired",
                    "expression": "request.time < timestamp('2020-01-01T00:00:00Z')",
                }
            payload = {"bindings": [binding]}
        elif command[:3] == ("pubsub", "subscriptions", "describe"):
            payload = {
                "name": SUBSCRIPTION_PATH,
                "topic": TOPIC_PATH,
                "state": "ACTIVE",
            }
            if self.push_subscription:
                payload["pushConfig"] = {
                    "pushEndpoint": "https://example.invalid/events"
                }
        elif command[:3] == (
            "pubsub",
            "subscriptions",
            "get-iam-policy",
        ):
            payload = {
                "bindings": [
                    {
                        "role": "roles/pubsub.subscriber",
                        "members": [f"serviceAccount:{GSA}"],
                    },
                    {
                        "role": "roles/pubsub.viewer",
                        "members": [f"serviceAccount:{GSA}"],
                    },
                ]
            }
            if self.omit_subscriber:
                payload = {"bindings": []}
        else:
            raise AssertionError(arguments)
        return CommandResult(0, stdout=json.dumps(payload))


class FakeRuntime:
    def __init__(
        self, conversations=(), *, truncated: bool = False, error=None
    ) -> None:
        self.conversations = conversations
        self.truncated = truncated
        self.error = error

    def list_conversations(self, agent, *, cutoff, limit=200):
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            conversations=self.conversations,
            truncated=self.truncated,
        )


class GoogleChatIntegrationServiceTest(unittest.TestCase):
    def test_ready_backend_comes_from_canonical_cr_and_exact_cloud_resources(self):
        cloud = FakeCloud()
        snapshot = GoogleChatIntegrationService(
            TARGET,
            kube=FakeKube(platform_agents(with_secret=True)),
            cloud=cloud,
            runtime=FakeRuntime(),
        ).inspect()

        self.assertEqual(snapshot["status"], "Ready")
        self.assertEqual(snapshot["severity"], "info")
        self.assertEqual(snapshot["configuration"]["topicPath"], TOPIC_PATH)
        self.assertEqual(
            snapshot["configuration"]["subscriptionPath"], SUBSCRIPTION_PATH
        )
        self.assertEqual(snapshot["configuration"]["agentServiceAccount"], GSA)
        self.assertEqual(snapshot["activity"]["sessionCount"], 0)
        self.assertEqual(snapshot["configuration"]["projectNumber"], "123456")
        self.assertEqual(
            snapshot["configuration"]["workspaceServiceAccount"],
            "service-123456@gcp-sa-gsuiteaddons.iam.gserviceaccount.com",
        )
        commands = [" ".join(arguments) for arguments in cloud.calls]
        self.assertTrue(all(TARGET.project_id in command for command in commands))
        self.assertTrue(any(TOPIC in command for command in commands))
        self.assertTrue(any(SUBSCRIPTION in command for command in commands))
        self.assertNotIn("literal-secret", snapshot["evidence"][0]["stdout"])
        self.assertIn("[REDACTED]", snapshot["evidence"][0]["stdout"])

    def test_missing_canonical_resource_lists_live_names_without_cloud_calls(self):
        cloud = FakeCloud()
        snapshot = GoogleChatIntegrationService(
            TARGET,
            kube=FakeKube(platform_agents(name="ux-e2e")),
            cloud=cloud,
            runtime=FakeRuntime(),
        ).inspect()

        self.assertEqual(snapshot["status"], "Needs attention")
        self.assertIn("ux-e2e", snapshot["message"])
        self.assertEqual(cloud.calls, [])

    def test_disabled_integration_does_not_query_cloud(self):
        cloud = FakeCloud()
        snapshot = GoogleChatIntegrationService(
            TARGET,
            kube=FakeKube(platform_agents(enabled=False)),
            cloud=cloud,
            runtime=FakeRuntime(),
        ).inspect()

        self.assertEqual(snapshot["status"], "Disabled")
        self.assertEqual(cloud.calls, [])

    def test_conditional_publisher_binding_is_not_claimed(self):
        snapshot = GoogleChatIntegrationService(
            TARGET,
            kube=FakeKube(platform_agents()),
            cloud=FakeCloud(conditional_publisher=True),
            runtime=FakeRuntime(),
        ).inspect()

        self.assertEqual(snapshot["status"], "Needs attention")
        failed = {
            check["id"]: check
            for check in snapshot["checks"]
            if check["status"] == "failed"
        }
        self.assertIn("topic_ready", failed)
        self.assertIn("missing", failed["topic_ready"]["detail"])

    def test_push_subscription_is_not_accepted_as_agent_backend(self):
        snapshot = GoogleChatIntegrationService(
            TARGET,
            kube=FakeKube(platform_agents()),
            cloud=FakeCloud(push_subscription=True),
            runtime=FakeRuntime(),
        ).inspect()

        self.assertEqual(snapshot["status"], "Needs attention")
        check = next(
            item
            for item in snapshot["checks"]
            if item["id"] == "subscription_ready"
        )
        self.assertEqual(check["status"], "failed")
        self.assertIn("pull subscription", check["detail"])

    def test_recent_google_chat_activity_is_reported_separately(self):
        last_active = datetime(2026, 8, 19, 20, tzinfo=UTC)
        runtime = FakeRuntime(
            (
                SimpleNamespace(platform="google_chat", last_active=last_active),
                SimpleNamespace(platform="portal", last_active=last_active),
            )
        )
        snapshot = GoogleChatIntegrationService(
            TARGET,
            kube=FakeKube(platform_agents()),
            cloud=FakeCloud(),
            runtime=runtime,
        ).inspect()

        self.assertEqual(snapshot["status"], "Ready")
        self.assertEqual(snapshot["severity"], "success")
        self.assertEqual(snapshot["activity"]["sessionCount"], 1)
        self.assertEqual(
            snapshot["activity"]["latestAt"], last_active.isoformat()
        )
        activity_check = next(
            check
            for check in snapshot["checks"]
            if check["id"] == "recent_activity"
        )
        self.assertEqual(activity_check["status"], "passed")
        self.assertFalse(activity_check["required"])

    def test_missing_subscriber_bindings_do_not_claim_agent_access(self):
        snapshot = GoogleChatIntegrationService(
            TARGET,
            kube=FakeKube(platform_agents()),
            cloud=FakeCloud(omit_subscriber=True),
            runtime=FakeRuntime(),
        ).inspect()

        check = next(
            item
            for item in snapshot["checks"]
            if item["id"] == "subscription_ready"
        )
        self.assertEqual(snapshot["status"], "Needs attention")
        self.assertEqual(check["status"], "failed")
        self.assertIn("missing", check["detail"])
        self.assertNotIn("can consume", check["detail"])

    def test_zero_delivery_with_stray_chat_topic_names_the_root_cause(self):
        cloud = FakeCloud(stray_topic=True)
        snapshot = GoogleChatIntegrationService(
            TARGET,
            kube=FakeKube(platform_agents()),
            cloud=cloud,
            runtime=FakeRuntime(),
        ).inspect()

        self.assertEqual(snapshot["status"], "Not receiving messages")
        self.assertEqual(snapshot["severity"], "error")
        self.assertIn("hermes-chat-events", snapshot["message"])
        self.assertEqual(
            snapshot["configuration"]["strayChatTopics"], [STRAY_TOPIC_PATH]
        )
        console_check = next(
            item
            for item in snapshot["checks"]
            if item["id"] == "chat_console_topic"
        )
        self.assertEqual(console_check["status"], "failed")
        self.assertFalse(console_check["required"])
        self.assertIn("hermes-chat-events", console_check["detail"])
        self.assertNotIn("unrelated-events", console_check["detail"])
        copy_values = [
            action.get("copy")
            for action in console_check["actions"]
            if action.get("copy")
        ]
        self.assertIn(TOPIC_PATH, copy_values)

    def test_zero_delivery_without_stray_topics_reports_unverifiable_console(
        self,
    ):
        snapshot = GoogleChatIntegrationService(
            TARGET,
            kube=FakeKube(platform_agents()),
            cloud=FakeCloud(),
            runtime=FakeRuntime(),
        ).inspect()

        console_check = next(
            item
            for item in snapshot["checks"]
            if item["id"] == "chat_console_topic"
        )
        self.assertEqual(console_check["status"], "unknown")
        self.assertEqual(snapshot["status"], "Ready")
        self.assertEqual(snapshot["severity"], "info")
        copy_values = [
            action.get("copy")
            for action in console_check["actions"]
            if action.get("copy")
        ]
        self.assertIn(TOPIC_PATH, copy_values)
        self.assertEqual(snapshot["configuration"]["strayChatTopics"], [])

    def test_observed_delivery_confirms_console_topic_without_stray_reads(self):
        cloud = FakeCloud(stray_topic=True)
        runtime = FakeRuntime(
            (
                SimpleNamespace(
                    platform="google_chat",
                    last_active=datetime(2026, 8, 19, 20, tzinfo=UTC),
                ),
            )
        )
        snapshot = GoogleChatIntegrationService(
            TARGET,
            kube=FakeKube(platform_agents()),
            cloud=cloud,
            runtime=runtime,
        ).inspect()

        console_check = next(
            item
            for item in snapshot["checks"]
            if item["id"] == "chat_console_topic"
        )
        self.assertEqual(console_check["status"], "passed")
        self.assertEqual(snapshot["configuration"]["strayChatTopics"], [])
        stray_reads = [
            arguments
            for arguments in cloud.calls
            if STRAY_TOPIC_PATH in arguments
        ]
        self.assertEqual(stray_reads, [])

    def test_failed_activity_read_is_not_reported_as_silence(self):
        from admin_console.agent_runtime import AgentRuntimeError

        cloud = FakeCloud(stray_topic=True)
        snapshot = GoogleChatIntegrationService(
            TARGET,
            kube=FakeKube(platform_agents()),
            cloud=cloud,
            runtime=FakeRuntime(error=AgentRuntimeError("gateway pod down")),
        ).inspect()

        self.assertEqual(snapshot["status"], "Verification incomplete")
        console_check = next(
            item
            for item in snapshot["checks"]
            if item["id"] == "chat_console_topic"
        )
        self.assertEqual(console_check["status"], "unknown")
        self.assertNotIn("No Google Chat message has arrived", snapshot["message"])
        stray_reads = [
            arguments
            for arguments in cloud.calls
            if STRAY_TOPIC_PATH in arguments
        ]
        self.assertEqual(stray_reads, [])

    def test_truncated_empty_activity_read_is_not_reported_as_silence(self):
        snapshot = GoogleChatIntegrationService(
            TARGET,
            kube=FakeKube(platform_agents()),
            cloud=FakeCloud(stray_topic=True),
            runtime=FakeRuntime(truncated=True),
        ).inspect()

        self.assertEqual(snapshot["status"], "Verification incomplete")
        console_check = next(
            item
            for item in snapshot["checks"]
            if item["id"] == "chat_console_topic"
        )
        self.assertEqual(console_check["status"], "unknown")

    def test_env_credential_names_are_redacted_in_evidence(self):
        payload = platform_agents()
        payload["items"][0]["spec"]["deployment"] = {
            "env": [
                {"name": "API_SERVER_KEY", "value": "gateway-key-literal"},
                {"name": "LITELLM_MASTER_KEY", "value": "master-key-literal"},
                {"name": "LOG_LEVEL", "value": "debug"},
            ]
        }
        snapshot = GoogleChatIntegrationService(
            TARGET,
            kube=FakeKube(payload),
            cloud=FakeCloud(),
            runtime=FakeRuntime(),
        ).inspect()

        stdout = snapshot["evidence"][0]["stdout"]
        self.assertNotIn("gateway-key-literal", stdout)
        self.assertNotIn("master-key-literal", stdout)
        self.assertIn("debug", stdout)

    def test_last_applied_configuration_annotation_is_scrubbed(self):
        payload = platform_agents()
        payload["items"][0]["metadata"]["annotations"] = {
            "kubectl.kubernetes.io/last-applied-configuration": json.dumps(
                {
                    "spec": {
                        "deployment": {
                            "env": [
                                {
                                    "name": "API_SERVER_KEY",
                                    "value": "LAST-APPLIED-LEAK",
                                },
                                {"name": "LOG_LEVEL", "value": "debug"},
                            ]
                        }
                    }
                }
            )
        }
        snapshot = GoogleChatIntegrationService(
            TARGET,
            kube=FakeKube(payload),
            cloud=FakeCloud(),
            runtime=FakeRuntime(),
        ).inspect()

        stdout = snapshot["evidence"][0]["stdout"]
        self.assertNotIn("LAST-APPLIED-LEAK", stdout)
        self.assertIn("[REDACTED]", stdout)
        self.assertIn("debug", stdout)

    def test_kubernetes_read_failure_is_not_reported_as_missing_resource(self):
        snapshot = GoogleChatIntegrationService(
            TARGET,
            kube=FakeKube({}, returncode=1),
            cloud=FakeCloud(),
            runtime=FakeRuntime(),
        ).inspect()

        self.assertEqual(snapshot["status"], "Verification incomplete")
        self.assertNotIn("was not found", snapshot["message"])


class FakeIntegration:
    def inspect(self):
        return {"status": "Backend ready", "configuration": {"topicPath": TOPIC_PATH}}


class GoogleChatIntegrationApiTest(unittest.TestCase):
    def test_api_uses_the_request_target(self):
        targets = []
        client = TestClient(
            create_app(
                google_chat_factory=lambda target: (
                    targets.append(target) or FakeIntegration()
                ),
                bound_target=TARGET,
            ),
            headers=portal_api_headers(),
        )

        response = client.get("/api/v1/integrations/google-chat")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["configuration"]["topicPath"], TOPIC_PATH)
        self.assertEqual(targets, [TARGET])

    def test_in_process_client_uses_the_bound_target(self):
        with patch(
            "admin_console.api.app.GoogleChatIntegrationService",
            side_effect=lambda target: FakeIntegration(),
        ):
            snapshot = PortalApiClient(TARGET).inspect_google_chat_integration()

        self.assertEqual(snapshot["status"], "Backend ready")


if __name__ == "__main__":
    unittest.main()
