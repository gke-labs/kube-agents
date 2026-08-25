"""LiteLLM catalog, raw evidence collection, and configuration operations."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from admin_console.agent_runtime import (
    GATEWAY_PYTHON,
    AgentRuntimeProvider,
    KubectlRunner,
)
from admin_console.kube_access import GKEKubeAccess, KubeCommandResult
from admin_console.project_config import (
    DeploymentTarget,
    is_valid_project_id,
    is_valid_region,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = (
    REPO_ROOT
    / "k8s-operator"
    / "config"
    / "integrations"
    / "litellm"
    / "providers.json"
)
LITELLM_ROOT = CATALOG_PATH.parent
OPERATOR_ROOT = REPO_ROOT / "k8s-operator"
MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,254}$")


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    default_model: str
    overlay: str
    authentication: dict[str, Any]
    settings: tuple[dict[str, str], ...] = ()
    workload_identity: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "defaultModel": self.default_model,
            "overlay": self.overlay,
            "authentication": self.authentication,
            "settings": list(self.settings),
            "workloadIdentity": self.workload_identity,
        }


class ProviderCatalog:
    """Read the repository-owned provider contract without UI defaults."""

    def __init__(self, path: Path = CATALOG_PATH) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schemaVersion") != 1:
            raise ValueError("unsupported LiteLLM provider catalog schema")
        self.default_provider = str(payload["defaultProvider"])
        self.gateway = dict(payload["gateway"])
        required_gateway_fields = {
            "deployment",
            "service",
            "configMapBase",
            "configVolume",
            "container",
            "servicePort",
            "containerPort",
            "readinessPath",
            "inferencePath",
            "modelAlias",
            "rolloutTimeoutSeconds",
        }
        if required_gateway_fields - self.gateway.keys():
            raise ValueError(
                "LiteLLM provider catalog has an incomplete gateway contract"
            )
        self.providers = tuple(
            Provider(
                id=str(row["id"]),
                label=str(row["label"]),
                default_model=str(row["defaultModel"]),
                overlay=str(row["overlay"]),
                authentication=dict(row["authentication"]),
                settings=tuple(row.get("settings", ())),
                workload_identity=(
                    dict(row["workloadIdentity"])
                    if row.get("workloadIdentity")
                    else None
                ),
            )
            for row in payload["providers"]
        )
        if self.default_provider not in {item.id for item in self.providers}:
            raise ValueError("default LiteLLM provider is not in the catalog")

    def get(self, provider_id: str) -> Provider:
        for provider in self.providers:
            if provider.id == provider_id:
                return provider
        raise ValueError(f"unsupported LiteLLM provider: {provider_id}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "defaultProvider": self.default_provider,
            "gateway": self.gateway,
            "providers": [provider.to_dict() for provider in self.providers],
        }


@dataclass(frozen=True)
class RawEvidence:
    source: str
    command: str
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @classmethod
    def from_result(
        cls, source: str, command: str, result: KubeCommandResult
    ) -> "RawEvidence":
        return cls(
            source,
            command,
            result.returncode,
            result.stdout,
            result.stderr,
            result.timed_out,
        )


class LlmGatewayService:
    """Operate only on the explicitly connected Kubernetes target."""

    def __init__(
        self,
        target: DeploymentTarget,
        *,
        kube: GKEKubeAccess | None = None,
        catalog: ProviderCatalog | None = None,
    ) -> None:
        self.target = target
        self.kube = kube or GKEKubeAccess(target)
        self.catalog = catalog or ProviderCatalog()

    def _run(
        self,
        source: str,
        arguments: list[str],
        *,
        timeout: int = 20,
        input_text: str | None = None,
    ) -> RawEvidence:
        arguments = ["--context", self.kube.context, *arguments]
        result = self.kube.run(arguments, timeout=timeout, input_text=input_text)
        return RawEvidence.from_result(source, "kubectl " + " ".join(arguments), result)

    def _current_configuration(
        self,
        deployment_evidence: RawEvidence,
        config_map_evidence: RawEvidence,
    ) -> dict[str, Any] | None:
        """Read the resolved provider/model from live, non-secret resources."""
        resolved = ""
        if deployment_evidence.returncode == 0:
            try:
                resolved = str(
                    json.loads(deployment_evidence.stdout)["spec"]["template"][
                        "metadata"
                    ]["annotations"]["kubeagents.x-k8s.io/model-config"]
                )
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
        if not resolved and config_map_evidence.returncode == 0:
            try:
                config = str(
                    json.loads(config_map_evidence.stdout)["data"]["config.yaml"]
                )
                match = re.search(
                    r"(?ms)^\s*-\s+model_name:\s*['\"]?model-default['\"]?\s*$"
                    r".*?^\s+model:\s*['\"]?([^'\"\s#]+)",
                    config,
                )
                resolved = match.group(1) if match else ""
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
        if "/" not in resolved:
            return None
        provider_id, model = resolved.split("/", 1)
        try:
            provider = self.catalog.get(provider_id)
        except ValueError:
            return None
        settings: dict[str, str] = {}
        if deployment_evidence.returncode == 0:
            try:
                containers = json.loads(deployment_evidence.stdout)["spec"][
                    "template"
                ]["spec"]["containers"]
                environment = {
                    str(item.get("name")): str(item.get("value"))
                    for container in containers
                    if container.get("name") == self.catalog.gateway["container"]
                    for item in container.get("env", [])
                    if item.get("name") and "value" in item
                }
                settings = {
                    str(item["id"]): environment[
                        str(item["deploymentEnvironmentVariable"])
                    ]
                    for item in provider.settings
                    if str(item["deploymentEnvironmentVariable"]) in environment
                }
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
        return {
            "providerId": provider.id,
            "providerLabel": provider.label,
            "model": model,
            "settings": settings,
        }

    def inspect(self) -> dict[str, Any]:
        """Return unclassified evidence from the live target as Kubernetes emits it."""
        namespace = self.target.namespace
        gateway = self.catalog.gateway
        deployment = str(gateway["deployment"])
        service = str(gateway["service"])
        deployment_evidence = self._run(
            f"deployment/{deployment}",
            ["get", "deployment", deployment, "-n", namespace, "-o", "json"],
        )
        config_map = str(gateway["configMapBase"])
        if deployment_evidence.returncode == 0:
            try:
                deployment_payload = json.loads(deployment_evidence.stdout)
                volumes = deployment_payload["spec"]["template"]["spec"].get(
                    "volumes", []
                )
                config_map = next(
                    str(volume["configMap"]["name"])
                    for volume in volumes
                    if volume.get("name") == gateway["configVolume"]
                    and volume.get("configMap", {}).get("name")
                )
            except (json.JSONDecodeError, KeyError, StopIteration, TypeError):
                pass
        container = str(gateway["container"])
        config_map_evidence = self._run(
            f"configmap/{config_map}",
            ["get", "configmap", config_map, "-n", namespace, "-o", "json"],
        )
        evidence = (
            deployment_evidence,
            self._run(
                f"service/{service}",
                ["get", "service", service, "-n", namespace, "-o", "json"],
            ),
            self._run(
                f"endpoints/{service}",
                ["get", "endpoints", service, "-n", namespace, "-o", "json"],
            ),
            config_map_evidence,
            self._run(
                "LiteLLM container logs",
                [
                    "logs",
                    f"deployment/{deployment}",
                    "-n",
                    namespace,
                    "-c",
                    container,
                    "--all-pods=true",
                    "--prefix=true",
                    "--tail=100",
                    "--limit-bytes=65536",
                ],
                timeout=30,
            ),
        )
        helm_managed = False
        if deployment_evidence.returncode == 0:
            try:
                helm_managed = self._is_helm_managed(
                    json.loads(deployment_evidence.stdout)
                )
            except (json.JSONDecodeError, TypeError):
                pass
        return {
            "target": asdict(self.target),
            "checkedAt": datetime.now(UTC).isoformat(),
            "catalog": self.catalog.to_dict(),
            "configuration": self._current_configuration(
                deployment_evidence, config_map_evidence
            ),
            "configurationWritable": not helm_managed,
            "configurationGuidance": (
                "This gateway is managed by Helm. Change its provider and model "
                "through the Terraform/Helm installation so the setting persists."
                if helm_managed
                else ""
            ),
            "evidence": [asdict(item) for item in evidence],
        }

    def status(self) -> dict[str, Any]:
        """Return resource evidence and the production-path request together."""
        snapshot = self.inspect()
        snapshot["verification"] = self.verify()["evidence"]
        return snapshot

    def verify(self) -> dict[str, Any]:
        """Call LiteLLM from a real Platform Agent and return the response unchanged."""
        gateway = self.catalog.gateway
        runtime = AgentRuntimeProvider(
            self.target,
            runner=KubectlRunner(self.target, access=self.kube),
        )
        agent = runtime.canonical_agent()
        pod, container = runtime.gateway_endpoint(agent)
        script = """import json,urllib.request,urllib.error
payload={'model':MODEL_ALIAS,'messages':[{'role':'user','content':'OK?'}],'max_tokens':8}
body=json.dumps(payload).encode()
request=urllib.request.Request(
  URL,data=body,headers={'Content-Type':'application/json'})
try:
  response=urllib.request.urlopen(request,timeout=60)
  print(response.status)
  print(response.read().decode('utf-8','replace'))
except urllib.error.HTTPError as error:
  print(error.code)
  print(error.read().decode('utf-8','replace'))
  raise SystemExit(1)
"""
        script = script.replace(
            "MODEL_ALIAS", repr(str(gateway["modelAlias"]))
        ).replace(
            "URL",
            repr(
                f"http://{gateway['service']}:{gateway['servicePort']}"
                f"{gateway['inferencePath']}"
            ),
        )
        evidence = self._run(
            f"POST {gateway['inferencePath']} from Platform Agent {agent}",
            [
                "exec",
                pod,
                "-n",
                self.target.namespace,
                "-c",
                container,
                "--",
                GATEWAY_PYTHON,
                "-c",
                script,
            ],
            timeout=75,
        )
        return {
            "target": asdict(self.target),
            "checkedAt": datetime.now(UTC).isoformat(),
            "evidence": asdict(evidence),
        }

    def _deployment(self, *, optional: bool = False) -> dict[str, Any] | None:
        arguments = [
            "--context",
            self.kube.context,
            "get",
            "deployment",
            str(self.catalog.gateway["deployment"]),
            "-n",
            self.target.namespace,
            "-o",
            "json",
        ]
        if optional:
            arguments.append("--ignore-not-found")
        result = self.kube.run(
            arguments
        )
        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.strip() or "LiteLLM Deployment is unavailable."
            )
        if optional and not result.stdout.strip():
            return None
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("LiteLLM Deployment returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("LiteLLM Deployment returned invalid JSON.")
        return payload

    def _secret_name(
        self,
        environment_variable: str,
        deployment: dict[str, Any] | None = None,
    ) -> str:
        deployment = deployment or self._deployment()
        if deployment is None:
            raise RuntimeError("LiteLLM Deployment is unavailable.")
        containers = deployment["spec"]["template"]["spec"].get("containers", [])
        for container in containers:
            for entry in container.get("env", []):
                if entry.get("name") != environment_variable:
                    continue
                reference = entry.get("valueFrom", {}).get("secretKeyRef", {})
                if reference.get("key") != environment_variable:
                    raise RuntimeError(
                        f"{environment_variable} uses an unexpected Secret key."
                    )
                if reference.get("name"):
                    return str(reference["name"])
        raise RuntimeError(
            "The live LiteLLM Deployment has no "
            f"{environment_variable} Secret reference."
        )

    def _restart(self) -> RawEvidence:
        restart_patch = json.dumps(
            {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "kubeagents.x-k8s.io/config-restarted-at": datetime.now(
                                    UTC
                                ).isoformat()
                            }
                        }
                    }
                }
            }
        )
        return self._run(
            f"deployment/{self.catalog.gateway['deployment']} restart",
            [
                "patch",
                "deployment",
                str(self.catalog.gateway["deployment"]),
                "-n",
                self.target.namespace,
                "--type=merge",
                "--patch-file=/dev/stdin",
            ],
            input_text=restart_patch,
        )

    def _write_credential(
        self,
        deployment: dict[str, Any],
        environment_variable: str,
        credential: str,
    ) -> RawEvidence:
        secret_name = self._secret_name(environment_variable, deployment)
        patch = json.dumps({"stringData": {environment_variable: credential}})
        return self._run(
            f"secret/{secret_name}",
            [
                "patch",
                "secret",
                secret_name,
                "-n",
                self.target.namespace,
                "--type=merge",
                "--patch-file=/dev/stdin",
            ],
            input_text=patch,
        )

    def _write_catalog_credential(
        self,
        provider: Provider,
        environment_variable: str,
        credential: str,
    ) -> RawEvidence:
        secret_name = str(provider.authentication.get("secretName") or "")
        if not secret_name:
            raise RuntimeError(
                f"{provider.label} has no credential Secret in the provider catalog."
            )
        patch = json.dumps({"stringData": {environment_variable: credential}})
        return self._run(
            f"secret/{secret_name}",
            [
                "patch",
                "secret",
                secret_name,
                "-n",
                self.target.namespace,
                "--type=merge",
                "--patch-file=/dev/stdin",
            ],
            input_text=patch,
        )

    @staticmethod
    def _is_helm_managed(deployment: dict[str, Any]) -> bool:
        metadata = deployment.get("metadata", {})
        labels = metadata.get("labels", {})
        annotations = metadata.get("annotations", {})
        managed_by = str(labels.get("app.kubernetes.io/managed-by", "")).lower()
        return managed_by == "helm" or bool(
            annotations.get("meta.helm.sh/release-name")
        )

    @classmethod
    def _assert_supported_install(cls, deployment: dict[str, Any] | None) -> None:
        if deployment is not None and cls._is_helm_managed(deployment):
            raise RuntimeError(
                "This LiteLLM Deployment is managed by Helm. The portal will not "
                "overwrite Helm-owned settings; update the Helm release instead."
            )

    @staticmethod
    def _iam_bindings(payload: str) -> set[tuple[str, str]]:
        try:
            document = json.loads(payload)
            return {
                (str(binding.get("role", "")), str(member))
                for binding in document.get("bindings", [])
                for member in binding.get("members", [])
            }
        except (AttributeError, json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError("Google Cloud IAM returned invalid JSON.") from exc

    @staticmethod
    def _gcloud(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        account = os.environ.get("KUBE_AGENTS_ADMIN_USER", "").strip()
        scoped_arguments = [*arguments]
        if account:
            scoped_arguments.append(f"--account={account}")
        try:
            return subprocess.run(
                ["gcloud", *scoped_arguments],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Could not run gcloud: {exc}") from exc

    def _assert_vertex_identity(
        self, provider: Provider, settings: dict[str, str]
    ) -> None:
        contract = provider.workload_identity or {}
        ksa = str(contract["kubernetesServiceAccount"])
        gsa_name = str(contract["googleServiceAccount"])
        gsa_email = f"{gsa_name}@{self.target.project_id}.iam.gserviceaccount.com"
        vertex_project = settings.get("project_id", self.target.project_id)
        required_api = str(contract["requiredApi"])
        project_role = str(contract["requiredProjectRole"])
        identity_role = str(contract["workloadIdentityRole"])
        identity_member = (
            f"serviceAccount:{self.target.project_id}.svc.id.goog"
            f"[{self.target.namespace}/{ksa}]"
        )

        missing: list[str] = []
        api = self._gcloud(
            [
                "services",
                "list",
                "--enabled",
                f"--project={vertex_project}",
                f"--filter=config.name={required_api}",
                "--format=value(config.name)",
            ]
        )
        if api.returncode != 0 or required_api not in api.stdout.splitlines():
            missing.append(f"enabled API {required_api} on {vertex_project}")

        service_account = self._gcloud(
            [
                "iam",
                "service-accounts",
                "describe",
                gsa_email,
                f"--project={self.target.project_id}",
                "--format=value(email)",
            ]
        )
        if service_account.returncode != 0 or gsa_email not in service_account.stdout:
            missing.append(f"service account {gsa_email}")
        else:
            identity_policy = self._gcloud(
                [
                    "iam",
                    "service-accounts",
                    "get-iam-policy",
                    gsa_email,
                    f"--project={self.target.project_id}",
                    "--format=json",
                ]
            )
            if identity_policy.returncode != 0 or (
                identity_role,
                identity_member,
            ) not in self._iam_bindings(identity_policy.stdout):
                missing.append(
                    f"{identity_role} binding for {self.target.namespace}/{ksa}"
                )

        project_policy = self._gcloud(
            [
                "projects",
                "get-iam-policy",
                vertex_project,
                "--format=json",
            ]
        )
        if project_policy.returncode != 0 or (
            project_role,
            f"serviceAccount:{gsa_email}",
        ) not in self._iam_bindings(project_policy.stdout):
            missing.append(f"{project_role} for {gsa_email} on {vertex_project}")

        if missing:
            raise RuntimeError(
                "Vertex AI prerequisites are incomplete: "
                + "; ".join(missing)
                + ". Update the Terraform/Helm installation's Vertex AI identity, "
                "then retry."
            )

    def device_status(self) -> dict[str, Any]:
        """Return device-flow rollout state and the latest authorization log."""
        deployment_evidence = self._run(
            f"deployment/{self.catalog.gateway['deployment']}",
            [
                "get",
                "deployment",
                str(self.catalog.gateway["deployment"]),
                "-n",
                self.target.namespace,
                "-o",
                "json",
            ],
        )
        log_evidence = self._run(
            "ChatGPT device authorization log",
            [
                "logs",
                f"deployment/{self.catalog.gateway['deployment']}",
                "-n",
                self.target.namespace,
                "-c",
                str(self.catalog.gateway["container"]),
                "--pod-running-timeout=3s",
                "--tail=100",
                "--limit-bytes=65536",
            ],
            timeout=5,
        )
        ready = False
        if deployment_evidence.returncode == 0:
            try:
                deployment = json.loads(deployment_evidence.stdout)
                desired = int(deployment.get("spec", {}).get("replicas", 1))
                status = deployment.get("status", {})
                ready = (
                    int(status.get("observedGeneration", 0))
                    >= int(deployment.get("metadata", {}).get("generation", 1))
                    and int(status.get("updatedReplicas", 0)) >= desired
                    and int(status.get("readyReplicas", 0)) >= desired
                    and int(status.get("availableReplicas", 0)) >= desired
                    and int(status.get("unavailableReplicas", 0)) == 0
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        return {
            "target": asdict(self.target),
            "checkedAt": datetime.now(UTC).isoformat(),
            "ready": ready,
            "evidence": [asdict(deployment_evidence), asdict(log_evidence)],
        }

    @staticmethod
    def _substitute(manifest: str, values: dict[str, str]) -> str:
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            return values.get(name, match.group(0))

        rendered = re.sub(r"\$\{([A-Z][A-Z0-9_]*)\}", replace, manifest)
        unresolved = sorted(set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", rendered)))
        if unresolved:
            raise RuntimeError(
                "Unresolved LiteLLM manifest values: " + ", ".join(unresolved)
            )
        return rendered

    def _render(self, provider: Provider, model: str, settings: dict[str, str]) -> str:
        renderer = OPERATOR_ROOT / "bin" / "kustomize"
        if not renderer.is_file():
            missing_tools = [
                name for name in ("make", "go") if shutil.which(name) is None
            ]
            if missing_tools:
                raise RuntimeError(
                    "LiteLLM configuration requires the repository renderer. "
                    "Install "
                    + " and ".join(missing_tools)
                    + ", or run `make -C k8s-operator kustomize` once."
                )
            ensure = subprocess.run(
                ["make", "-C", str(OPERATOR_ROOT), "kustomize"],
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if ensure.returncode != 0:
                raise RuntimeError(
                    ensure.stderr.strip() or "Kustomize is unavailable."
                )
        overlay = LITELLM_ROOT / (
            "base" if provider.overlay == "base" else f"overlays/{provider.overlay}"
        )
        build = subprocess.run(
            [str(OPERATOR_ROOT / "bin" / "kustomize"), "build", str(overlay)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if build.returncode != 0:
            raise RuntimeError(
                build.stderr.strip() or "LiteLLM manifests did not render."
            )
        inventory = json.loads((REPO_ROOT / "images.json").read_text(encoding="utf-8"))
        image = next(row for row in inventory["images"] if row["name"] == "litellm")
        values = {
            "NAMESPACE": self.target.namespace,
            "MODEL_PROVIDER": provider.id,
            "MODEL_DEFAULT_NAME": model,
            "LITELLM_IMAGE": f"{image['repository']}:{image['tag']}",
            "PROJECT_ID": self.target.project_id,
            "VERTEX_PROJECT_ID": settings.get("project_id", self.target.project_id),
            # Not the target's region: a model is only callable from a location
            # that serves it, and the cluster's often is not one. Mirrors
            # DEFAULT_VERTEX_LOCATION in k8s-operator/scripts/installer_common.sh.
            "VERTEX_LOCATION": settings.get("location") or "global",
        }
        if provider.workload_identity:
            values["LITELLM_KSA_NAME"] = provider.workload_identity[
                "kubernetesServiceAccount"
            ]
            values["LITELLM_GSA_NAME"] = provider.workload_identity[
                "googleServiceAccount"
            ]
        return self._substitute(build.stdout, values)

    def configure(
        self,
        provider_id: str,
        model: str,
        *,
        credential: str = "",
        settings: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        provider = self.catalog.get(provider_id)
        model = model.strip()
        if not MODEL_PATTERN.fullmatch(model):
            raise ValueError("Model name contains unsupported characters.")
        settings = {key: value.strip() for key, value in (settings or {}).items()}
        allowed_settings = {str(item["id"]) for item in provider.settings}
        unknown_settings = sorted(set(settings) - allowed_settings)
        if unknown_settings:
            raise ValueError(
                "Unsupported provider settings: " + ", ".join(unknown_settings)
            )
        if "project_id" in settings and not is_valid_project_id(settings["project_id"]):
            raise ValueError("Vertex project is not a valid Google Cloud project ID.")
        if (
            "location" in settings
            and settings["location"] != "global"
            and not is_valid_region(settings["location"])
        ):
            raise ValueError("Vertex location must be a region or global.")
        evidence: list[RawEvidence] = []
        auth = provider.authentication
        before = self._deployment(optional=True)
        self._assert_supported_install(before)
        if provider.authentication["type"] == "workload_identity":
            self._assert_vertex_identity(provider, settings)
        manifest = self._render(provider, model, settings)

        variable = ""
        if auth["type"] == "api_key" and credential:
            variable = str(auth["environmentVariable"])
            evidence.append(
                self._write_credential(before, variable, credential)
                if before is not None
                else self._write_catalog_credential(provider, variable, credential)
            )
            if evidence[-1].returncode != 0:
                return {
                    "configurationApplied": False,
                    "evidence": [asdict(item) for item in evidence],
                }

        evidence.append(
            self._run(
                "LiteLLM manifests",
                ["apply", "-n", self.target.namespace, "-f", "-"],
                input_text=manifest,
                timeout=60,
            )
        )
        if evidence[-1].returncode != 0:
            return {
                "configurationApplied": False,
                "evidence": [asdict(item) for item in evidence],
            }
        after = self._deployment()
        if after is None:
            raise RuntimeError("LiteLLM Deployment is unavailable after apply.")
        template_changed = before is None or (
            before.get("spec", {}).get("template")
            != after.get("spec", {}).get("template")
        )
        if not template_changed:
            evidence.append(self._restart())
            if evidence[-1].returncode != 0:
                return {
                    "configurationApplied": False,
                    "evidence": [asdict(item) for item in evidence],
                }
        if provider.authentication["type"] == "device_oauth":
            evidence.append(
                self._run(
                    "ChatGPT device authorization log",
                    [
                        "logs",
                        f"deployment/{self.catalog.gateway['deployment']}",
                        "-n",
                        self.target.namespace,
                        "-c",
                        str(self.catalog.gateway["container"]),
                        "--pod-running-timeout=45s",
                        "--tail=100",
                        "--limit-bytes=65536",
                    ],
                    timeout=50,
                )
            )
        else:
            rollout_timeout = int(self.catalog.gateway["rolloutTimeoutSeconds"])
            evidence.append(
                self._run(
                    f"deployment/{self.catalog.gateway['deployment']} rollout",
                    [
                        "rollout",
                        "status",
                        f"deployment/{self.catalog.gateway['deployment']}",
                        "-n",
                        self.target.namespace,
                        f"--timeout={rollout_timeout}s",
                    ],
                    timeout=rollout_timeout + 10,
                )
            )
        return {
            "target": asdict(self.target),
            "provider": provider.to_dict(),
            "model": model,
            "configurationApplied": True,
            "readyForTest": (
                provider.authentication["type"] != "device_oauth"
                and evidence[-1].returncode == 0
            ),
            "evidence": [asdict(item) for item in evidence],
        }
