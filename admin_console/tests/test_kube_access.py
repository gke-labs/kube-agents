from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from admin_console.kube_access import (
    GKEKubeAccess,
    KubeFailure,
    PrivateKubeconfigStore,
    kube_failure_guidance,
)
from admin_console.project_config import DeploymentTarget


TARGET = DeploymentTarget(
    "test-project-01",
    "test-cluster-01",
    "us-east4",
    "kubeagents-system",
)


class GKEKubeAccessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.kubeconfig = Path(self.temporary_directory.name) / "config"
        self.store = PrivateKubeconfigStore(self.kubeconfig)

    @staticmethod
    def completed(
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], returncode, stdout, stderr)

    @patch("admin_console.kube_access.shutil.which", return_value="/usr/bin/tool")
    @patch("admin_console.kube_access.subprocess.run")
    def test_missing_context_is_prepared_in_private_kubeconfig(
        self,
        run,
        _which,
    ) -> None:
        def result(command, **_kwargs):
            if command[:3] == ["kubectl", "config", "get-contexts"]:
                return self.completed(1)
            if command[0] == "gcloud":
                self.kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
                return self.completed()
            return self.completed(stdout='{"items": []}')

        run.side_effect = result
        access = GKEKubeAccess(
            TARGET,
            account="admin@example.com",
            store=self.store,
        )

        command_result = access.run(["get", "pods"])

        self.assertEqual(command_result.returncode, 0)
        credential_command = run.call_args_list[1].args[0]
        self.assertEqual(
            credential_command,
            [
                "gcloud",
                "--quiet",
                "--account=admin@example.com",
                "container",
                "clusters",
                "get-credentials",
                TARGET.cluster_name,
                "--location",
                TARGET.location,
                "--project",
                TARGET.project_id,
            ],
        )
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["env"]["KUBECONFIG"], str(self.kubeconfig))
            self.assertEqual(
                call.kwargs["env"]["CLOUDSDK_CORE_ACCOUNT"],
                "admin@example.com",
            )
        self.assertEqual(self.kubeconfig.stat().st_mode & 0o777, 0o600)

    @patch("admin_console.kube_access.shutil.which", return_value="/usr/bin/tool")
    @patch("admin_console.kube_access.subprocess.run")
    def test_existing_context_skips_credential_refresh(self, run, _which) -> None:
        run.side_effect = (
            self.completed(stdout="gke_test-project-01_us-east4_test-cluster-01\n"),
            self.completed(stdout='{"items": []}'),
        )
        access = GKEKubeAccess(TARGET, store=self.store)

        result = access.run(["get", "pods"])

        self.assertEqual(result.returncode, 0)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[-1].args[0], ["kubectl", "get", "pods"])

    @patch("admin_console.kube_access.shutil.which", return_value="/usr/bin/tool")
    @patch("admin_console.kube_access.subprocess.run")
    def test_credential_failure_is_typed_and_sanitized(self, run, _which) -> None:
        run.side_effect = (
            self.completed(1),
            self.completed(1, stderr="403 forbidden secret-token-value"),
        )
        access = GKEKubeAccess(TARGET, store=self.store)

        result = access.run(["get", "pods"])

        self.assertEqual(result.failure, KubeFailure.GKE_ACCESS_DENIED)
        self.assertEqual(result.stderr, "GKE credential access was denied.")
        self.assertNotIn("secret-token-value", result.stderr)
        self.assertEqual(
            kube_failure_guidance(result),
            "Request permission to access the selected GKE cluster, then retry.",
        )

    @patch("admin_console.kube_access.shutil.which")
    def test_missing_gcloud_has_an_actionable_failure(self, which) -> None:
        which.side_effect = lambda command: (
            "/usr/bin/kubectl" if command == "kubectl" else None
        )
        access = GKEKubeAccess(TARGET, store=self.store)

        result = access.run(["get", "pods"])

        self.assertEqual(result.failure, KubeFailure.GCLOUD_MISSING)
        self.assertEqual(
            kube_failure_guidance(result),
            "Install the Google Cloud CLI, then retry.",
        )

    @patch("admin_console.kube_access.subprocess.Popen")
    def test_streaming_output_uses_the_same_prepared_access(self, popen) -> None:
        class Input:
            def __init__(self) -> None:
                self.value = ""

            def write(self, value: str) -> None:
                self.value += value

            def close(self) -> None:
                pass

        class Process:
            def __init__(self) -> None:
                self.stdin = Input()
                self.stdout = io.StringIO("first\nsecond\n")
                self.stderr = io.StringIO("")
                self.returncode = 0

            def wait(self, timeout=None) -> None:
                pass

        process = Process()
        popen.return_value = process
        access = GKEKubeAccess(TARGET, store=self.store)
        lines = []

        with patch.object(access, "prepare", return_value=None):
            result = access.run(
                ["exec", "pod"],
                input_text="payload",
                line_callback=lines.append,
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "first\nsecond\n")
        self.assertEqual(lines, ["first", "second"])
        self.assertEqual(process.stdin.value, "payload")
        self.assertEqual(popen.call_args.args[0], ["kubectl", "exec", "pod"])


if __name__ == "__main__":
    unittest.main()
