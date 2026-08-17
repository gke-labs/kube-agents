"""Shared, process-private Kubernetes access for a selected GKE target."""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable

from admin_console.project_config import (
    DeploymentTarget,
    is_valid_cluster_name,
    is_valid_location,
    is_valid_namespace,
    is_valid_project_id,
)


class KubeFailure(StrEnum):
    NONE = ""
    KUBECTL_MISSING = "kubectl_missing"
    GCLOUD_MISSING = "gcloud_missing"
    AUTH_PLUGIN_MISSING = "auth_plugin_missing"
    GKE_ACCESS_DENIED = "gke_access_denied"
    CLUSTER_NOT_FOUND = "cluster_not_found"
    AUTH_EXPIRED = "auth_expired"
    CREDENTIAL_SETUP = "credential_setup"


@dataclass(frozen=True)
class KubeCommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    failure: KubeFailure = KubeFailure.NONE


def kube_failure_guidance(result: KubeCommandResult) -> str:
    """Map typed preparation failures to one safe, actionable sentence."""
    return {
        KubeFailure.KUBECTL_MISSING: "Install kubectl, then retry.",
        KubeFailure.GCLOUD_MISSING: "Install the Google Cloud CLI, then retry.",
        KubeFailure.AUTH_PLUGIN_MISSING: (
            "Install gke-gcloud-auth-plugin, then retry."
        ),
        KubeFailure.GKE_ACCESS_DENIED: (
            "Request permission to access the selected GKE cluster, then retry."
        ),
        KubeFailure.CLUSTER_NOT_FOUND: (
            "Confirm the selected project, cluster, and location, then retry."
        ),
        KubeFailure.AUTH_EXPIRED: "Refresh gcloud authentication, then retry.",
        KubeFailure.CREDENTIAL_SETUP: (
            "Check GKE access and network connectivity, then retry."
        ),
    }.get(result.failure, "")


class PrivateKubeconfigStore:
    """Own one temporary kubeconfig for the lifetime of this portal process."""

    def __init__(self, path: Path | None = None) -> None:
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        if path is None:
            self._temporary_directory = tempfile.TemporaryDirectory(
                prefix="kube-agents-admin-"
            )
            path = Path(self._temporary_directory.name) / "config"
        self.path = path
        self._lock = threading.Lock()

    @property
    def lock(self) -> threading.Lock:
        return self._lock

    def secure(self) -> None:
        if self.path.is_file():
            self.path.chmod(0o600)

    def close(self) -> None:
        """Remove a managed temporary kubeconfig; explicit paths are retained."""
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()
            self._temporary_directory = None


_SHARED_KUBECONFIG = PrivateKubeconfigStore()
atexit.register(_SHARED_KUBECONFIG.close)


class GKEKubeAccess:
    """Prepare and run kubectl against one validated GKE target.

    The selected target is always explicit, credentials are written only to a
    process-private kubeconfig, and both buffered and streaming calls share the
    same preparation and failure behavior.
    """

    def __init__(
        self,
        target: DeploymentTarget,
        *,
        account: str = "",
        store: PrivateKubeconfigStore | None = None,
    ) -> None:
        if not (
            is_valid_project_id(target.project_id)
            and is_valid_cluster_name(target.cluster_name)
            and is_valid_location(target.location)
            and is_valid_namespace(target.namespace)
        ):
            raise ValueError("invalid GKE access target")
        self.target = target
        self.account = account or os.environ.get(
            "KUBE_AGENTS_ADMIN_USER", ""
        ).strip()
        self.store = store or _SHARED_KUBECONFIG
        self.context = (
            f"gke_{target.project_id}_{target.location}_{target.cluster_name}"
        )
        self._prepared = False

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["KUBECONFIG"] = str(self.store.path)
        if self.account:
            environment["CLOUDSDK_CORE_ACCOUNT"] = self.account
        return environment

    @staticmethod
    def _missing_prerequisite() -> KubeCommandResult | None:
        if shutil.which("kubectl") is None:
            return KubeCommandResult(
                127,
                stderr="Kubernetes CLI is unavailable.",
                failure=KubeFailure.KUBECTL_MISSING,
            )
        if shutil.which("gcloud") is None:
            return KubeCommandResult(
                127,
                stderr="Google Cloud CLI is unavailable.",
                failure=KubeFailure.GCLOUD_MISSING,
            )
        if shutil.which("gke-gcloud-auth-plugin") is None:
            return KubeCommandResult(
                127,
                stderr="GKE authentication plugin is unavailable.",
                failure=KubeFailure.AUTH_PLUGIN_MISSING,
            )
        return None

    @staticmethod
    def _completed(
        completed: subprocess.CompletedProcess[str],
    ) -> KubeCommandResult:
        return KubeCommandResult(
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )

    def prepare(self, *, timeout: int = 30) -> KubeCommandResult | None:
        """Ensure the private kubeconfig contains this target's context."""
        if self._prepared:
            return None
        missing = self._missing_prerequisite()
        if missing is not None:
            return missing
        environment = self._environment()
        with self.store.lock:
            try:
                context_probe = subprocess.run(
                    [
                        "kubectl",
                        "config",
                        "get-contexts",
                        self.context,
                        "-o",
                        "name",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env=environment,
                )
            except subprocess.TimeoutExpired:
                return KubeCommandResult(
                    124,
                    timed_out=True,
                    failure=KubeFailure.CREDENTIAL_SETUP,
                )
            except OSError:
                return KubeCommandResult(
                    127,
                    stderr="Kubernetes CLI is unavailable.",
                    failure=KubeFailure.KUBECTL_MISSING,
                )
            if context_probe.returncode != 0 or context_probe.stdout.strip() != self.context:
                command = ["gcloud", "--quiet"]
                if self.account:
                    command.append(f"--account={self.account}")
                command.extend(
                    [
                        "container",
                        "clusters",
                        "get-credentials",
                        self.target.cluster_name,
                        "--location",
                        self.target.location,
                        "--project",
                        self.target.project_id,
                    ]
                )
                try:
                    credentials = subprocess.run(
                        command,
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                        env=environment,
                    )
                except subprocess.TimeoutExpired:
                    return KubeCommandResult(
                        124,
                        timed_out=True,
                        failure=KubeFailure.CREDENTIAL_SETUP,
                    )
                except OSError:
                    return KubeCommandResult(
                        127,
                        stderr="Google Cloud CLI is unavailable.",
                        failure=KubeFailure.GCLOUD_MISSING,
                    )
                if credentials.returncode != 0:
                    error = credentials.stderr.lower()
                    if "permission" in error or "forbidden" in error or "403" in error:
                        detail = "GKE credential access was denied."
                        failure = KubeFailure.GKE_ACCESS_DENIED
                    elif "not found" in error or "does not exist" in error:
                        detail = "The selected GKE cluster was not found."
                        failure = KubeFailure.CLUSTER_NOT_FOUND
                    elif "login" in error or "reauth" in error or "invalid_grant" in error:
                        detail = "Google Cloud authentication expired."
                        failure = KubeFailure.AUTH_EXPIRED
                    else:
                        detail = "GKE credentials could not be prepared."
                        failure = KubeFailure.CREDENTIAL_SETUP
                    return KubeCommandResult(
                        credentials.returncode,
                        stderr=detail,
                        failure=failure,
                    )
                self.store.secure()
            self._prepared = True
        return None

    def run(
        self,
        arguments: list[str],
        *,
        input_text: str | None = None,
        timeout: int = 20,
        line_callback: Callable[[str], None] | None = None,
    ) -> KubeCommandResult:
        preparation = self.prepare(timeout=min(timeout, 30))
        if preparation is not None:
            return preparation
        command = ["kubectl", *arguments]
        environment = self._environment()
        if line_callback is None:
            try:
                completed = subprocess.run(
                    command,
                    input=input_text,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=environment,
                )
            except subprocess.TimeoutExpired:
                return KubeCommandResult(124, timed_out=True)
            except OSError:
                return KubeCommandResult(
                    127,
                    stderr="Kubernetes CLI is unavailable.",
                    failure=KubeFailure.KUBECTL_MISSING,
                )
            return self._completed(completed)

        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
        except OSError:
            return KubeCommandResult(
                127,
                stderr="Kubernetes CLI is unavailable.",
                failure=KubeFailure.KUBECTL_MISSING,
            )

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        def drain_stdout() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                stdout_lines.append(line)
                line_callback(line.rstrip("\r\n"))

        def drain_stderr() -> None:
            assert process.stderr is not None
            stderr_lines.extend(process.stderr)

        stdout_thread = threading.Thread(target=drain_stdout, daemon=True)
        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        assert process.stdin is not None
        try:
            process.stdin.write(input_text or "")
            process.stdin.close()
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)
            return KubeCommandResult(
                124,
                "".join(stdout_lines),
                "".join(stderr_lines),
                timed_out=True,
            )
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)
        return KubeCommandResult(
            process.returncode,
            "".join(stdout_lines),
            "".join(stderr_lines),
        )
