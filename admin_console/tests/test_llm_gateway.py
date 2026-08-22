from __future__ import annotations

import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from admin_console.api.authorization import portal_api_headers
from admin_console.api.app import create_app
from admin_console.kube_access import KubeCommandResult
from admin_console.llm_gateway import LlmGatewayService
from admin_console.project_config import (
    DeploymentTarget,
    deployment_target_headers,
)

TARGET = DeploymentTarget(
    "test-project-01",
    "test-cluster-01",
    "us-east4",
    "kubeagents-system",
)


def deployment(provider: str = "gemini", model: str = "gemini-3.5-flash"):
    return {
        "metadata": {"generation": 2},
        "spec": {
            "replicas": 1,
            "template": {
                "metadata": {
                    "annotations": {
                        "kubeagents.x-k8s.io/model-config": f"{provider}/{model}"
                    }
                },
                "spec": {
                    "containers": [
                        {
                            "env": [
                                {
                                    "name": "GEMINI_API_KEY",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": "custom-credentials",
                                            "key": "GEMINI_API_KEY",
                                        }
                                    },
                                }
                            ]
                        }
                    ]
                },
            },
        },
        "status": {
            "observedGeneration": 2,
            "updatedReplicas": 1,
            "readyReplicas": 1,
            "availableReplicas": 1,
        },
    }


class FakeKube:
    def __init__(self, results: list[KubeCommandResult]) -> None:
        self.results = list(results)
        self.calls: list[tuple[list[str], str | None]] = []
        self.context = (
            f"gke_{TARGET.project_id}_{TARGET.location}_{TARGET.cluster_name}"
        )

    def run(self, arguments, *, input_text=None, timeout=20, line_callback=None):
        self.calls.append((list(arguments), input_text))
        return self.results.pop(0)


class LlmGatewayServiceTest(unittest.TestCase):

    def test_inspect_returns_raw_results_without_classification(self):
        results = [
            KubeCommandResult(0, stdout=f"raw-{index}", stderr=f"error-{index}")
            for index in range(5)
        ]
        service = LlmGatewayService(TARGET, kube=FakeKube(results))

        snapshot = service.inspect()

        self.assertEqual(len(snapshot["evidence"]), 5)
        self.assertEqual(snapshot["evidence"][0]["stdout"], "raw-0")
        self.assertEqual(snapshot["evidence"][0]["stderr"], "error-0")
        self.assertNotIn("status", snapshot["evidence"][0])
        self.assertNotIn("message", snapshot["evidence"][0])



    def test_api_key_is_sent_on_stdin_and_never_in_arguments_or_response(self):
        live_deployment = deployment()
        kube = FakeKube(
            [
                KubeCommandResult(0, json.dumps(live_deployment)),
                KubeCommandResult(0, "secret patched"),
                KubeCommandResult(0, "resources applied"),
                KubeCommandResult(0, json.dumps(live_deployment)),
                KubeCommandResult(0, "deployment patched"),
                KubeCommandResult(0, "rollout complete"),
            ]
        )
        service = LlmGatewayService(TARGET, kube=kube)

        with patch.object(service, "_render", return_value="rendered manifest"):
            result = service.configure(
                "gemini", "gemini-3.5-flash", credential="super-secret"
            )

        all_arguments = " ".join(" ".join(call[0]) for call in kube.calls)
        self.assertNotIn("super-secret", all_arguments)
        self.assertNotIn("super-secret", json.dumps(result))
        patch_call = kube.calls[1]
        self.assertIn("super-secret", patch_call[1])
        self.assertIn("custom-credentials", patch_call[0])


    def test_helm_owned_deployment_is_not_modified(self):
        live_deployment = deployment()
        live_deployment["metadata"]["labels"] = {
            "app.kubernetes.io/managed-by": "Helm"
        }
        kube = FakeKube([KubeCommandResult(0, json.dumps(live_deployment))])
        service = LlmGatewayService(TARGET, kube=kube)

        with self.assertRaisesRegex(RuntimeError, "managed by Helm"):
            service.configure(
                "gemini", "gemini-3.5-flash", credential="never-written"
            )

        self.assertEqual(len(kube.calls), 1)



    def test_render_failure_does_not_change_credential(self):
        kube = FakeKube([KubeCommandResult(0, json.dumps(deployment()))])
        service = LlmGatewayService(TARGET, kube=kube)

        with (
            patch.object(service, "_render", side_effect=RuntimeError("bad render")),
            self.assertRaisesRegex(RuntimeError, "bad render"),
        ):
            service.configure(
                "gemini", "gemini-3.5-flash", credential="never-written"
            )

        self.assertEqual(len(kube.calls), 1)

    def test_vertex_switch_requires_complete_live_identity(self):
        live_deployment = deployment()
        kube = FakeKube([KubeCommandResult(0, json.dumps(live_deployment))])
        service = LlmGatewayService(TARGET, kube=kube)
        failed = subprocess.CompletedProcess(
            ["gcloud"], 1, stdout="", stderr="permission denied"
        )

        with (
            patch.object(service, "_gcloud", return_value=failed),
            self.assertRaisesRegex(RuntimeError, "prerequisites are incomplete"),
        ):
            service.configure(
                "vertex_ai",
                "claude-sonnet-4-5@20250929",
                settings={"project_id": TARGET.project_id, "location": "global"},
            )

        self.assertEqual(len(kube.calls), 1)


    def test_verification_uses_production_service_path_and_returns_raw_error(self):
        pods = {
            "items": [
                {
                    "metadata": {
                        "name": "platform-agent-pod",
                        "labels": {"app": "platform-agent-gateway"},
                    },
                    "spec": {
                        "containers": [
                            {
                                "name": "runtime-from-pod",
                                "ports": [{"name": "api", "containerPort": 8642}],
                            }
                        ]
                    },
                }
            ]
        }
        kube = FakeKube(
            [
                KubeCommandResult(
                    0,
                    json.dumps(
                        {"items": [{"metadata": {"name": "platform-agent"}}]}
                    ),
                ),
                KubeCommandResult(0, json.dumps(pods)),
                KubeCommandResult(1, "401\nraw upstream response"),
            ]
        )
        service = LlmGatewayService(TARGET, kube=kube)

        result = service.verify()

        script = kube.calls[2][0][-1]
        self.assertIn("http://litellm:80/v1/chat/completions", script)
        self.assertIn("platform-agent-pod", kube.calls[2][0])
        self.assertIn("runtime-from-pod", kube.calls[2][0])
        self.assertEqual(result["evidence"]["returncode"], 1)
        self.assertEqual(result["evidence"]["stdout"], "401\nraw upstream response")

    def test_chatgpt_changed_configuration_uses_one_rollout_and_returns_device_log(self):
        kube = FakeKube(
            [
                KubeCommandResult(0, json.dumps(deployment())),
                KubeCommandResult(0, "resources applied"),
                KubeCommandResult(0, json.dumps(deployment("chatgpt", "gpt-5.4"))),
                KubeCommandResult(0, "Visit URL\nEnter code: ABCD-EFGH"),
            ]
        )
        service = LlmGatewayService(TARGET, kube=kube)

        with patch.object(service, "_render", return_value="rendered manifest"):
            result = service.configure("chatgpt", "gpt-5.4")

        commands = [call[0] for call in kube.calls]
        self.assertFalse(any(command[2:4] == ["rollout", "status"] for command in commands))
        self.assertFalse(any(command[2:4] == ["patch", "deployment"] for command in commands))
        self.assertEqual(commands[-1][2], "logs")
        self.assertIn("--pod-running-timeout=45s", commands[-1])
        self.assertIn("ABCD-EFGH", result["evidence"][-1]["stdout"])

    def test_every_kubectl_command_pins_the_connected_context(self):
        live_deployment = deployment()
        kube = FakeKube(
            [
                KubeCommandResult(0, json.dumps(live_deployment)),
                KubeCommandResult(0, "secret patched"),
                KubeCommandResult(0, "resources applied"),
                KubeCommandResult(0, json.dumps(live_deployment)),
                KubeCommandResult(0, "deployment patched"),
                KubeCommandResult(0, "rollout complete"),
            ]
        )
        service = LlmGatewayService(TARGET, kube=kube)

        with patch.object(service, "_render", return_value="rendered manifest"):
            service.configure(
                "gemini", "gemini-3.5-flash", credential="super-secret"
            )

        for arguments, _ in kube.calls:
            self.assertEqual(arguments[:2], ["--context", kube.context])


class FakeGateway:
    def status(self):
        return {
            "evidence": [{"stdout": "raw"}],
            "verification": {"stdout": "401 upstream"},
        }

    def device_status(self):
        return {"ready": False, "evidence": {"stdout": "pending"}}

    def configure(self, provider_id, model, *, credential="", settings=None):
        return {
            "provider": provider_id,
            "model": model,
            "credentialReceived": bool(credential),
            "settings": settings,
        }


class LlmGatewayApiTest(unittest.TestCase):
    def setUp(self):
        self.connection = SimpleNamespace(usable=True, target=TARGET)
        loader = patch(
            "admin_console.api.app.load_connection", return_value=self.connection
        )
        loader.start()
        self.addCleanup(loader.stop)
        self.client = TestClient(
            create_app(llm_gateway_factory=lambda target: FakeGateway()),
            headers=portal_api_headers(),
        )
        self.headers = deployment_target_headers(TARGET)

    def test_status_includes_resources_and_verification(self):
        status = self.client.get("/api/v1/llm-gateway", headers=self.headers)
        device_status = self.client.get(
            "/api/v1/llm-gateway/device-status", headers=self.headers
        )

        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["evidence"][0]["stdout"], "raw")
        self.assertEqual(
            status.json()["verification"]["stdout"], "401 upstream"
        )
        self.assertFalse(device_status.json()["ready"])


    def test_configuration_validation_never_echoes_rejected_credential(self):
        marker = "SENSITIVE_MARKER_"
        response = self.client.post(
            "/api/v1/llm-gateway/configuration",
            headers=self.headers,
            json={
                "providerId": "gemini",
                "model": "gemini-3.5-flash",
                "credential": marker + "x" * 16_384,
                "settings": {},
            },
        )

        self.assertEqual(response.status_code, 422)
        self.assertNotIn(marker, response.text)
        self.assertIn("credential", response.text)

if __name__ == "__main__":
    unittest.main()
