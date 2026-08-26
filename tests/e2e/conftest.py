"""Shared fixtures and helpers for live GKE E2E promotion tests."""

import base64
import json
import os
import pathlib
import subprocess
import time
from collections.abc import Generator
from typing import Any, Dict, Optional

import pytest

try:
    from dotenv import load_dotenv
    _repo_root_env = pathlib.Path(__file__).resolve().parents[2] / ".env"
    if _repo_root_env.is_file():
        load_dotenv(_repo_root_env)
except ImportError:
    pass

try:
    import yaml
except ImportError:
    yaml = None

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_PATH = _REPO_ROOT / "tests" / "e2e" / "e2e_config.yaml"


def pytest_configure(config: pytest.Config) -> None:
    """Configures session environment variables and ensures gke auth plugin settings."""
    if "CLOUDSDK_PYTHON" in os.environ:
        del os.environ["CLOUDSDK_PYTHON"]
    os.environ["CLOUDSDK_PYTHON_SITEPACKAGES"] = "0"
    os.environ["PYTHONNOUSERSITE"] = "1"
    if "USE_GKE_GCLOUD_AUTH_PLUGIN" not in os.environ:
        os.environ["USE_GKE_GCLOUD_AUTH_PLUGIN"] = "True"
    os.environ["CLOUDSDK_CONTAINER_USE_APPLICATION_DEFAULT_CREDENTIALS"] = "false"


def _parse_yaml_fallback(content: str) -> Dict[str, Any]:
    """Fallback parser for simple environments list and env_vars in e2e_config.yaml."""
    result: Dict[str, Any] = {"defaults": {}, "environments": []}
    current_env: Optional[Dict[str, Any]] = None
    in_env_vars = False
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- name:"):
            in_env_vars = False
            if current_env:
                result["environments"].append(current_env)
            name = stripped.split(":", 1)[1].strip().strip('"\'')
            current_env = {"name": name, "tests": [], "env_vars": {}}
        elif current_env and stripped.startswith("- ") and "tests/" in stripped:
            in_env_vars = False
            current_env["tests"].append(stripped.lstrip("- ").strip().strip('"\''))
        elif current_env and stripped.startswith("env_vars:"):
            in_env_vars = True
        elif current_env and in_env_vars and ":" in stripped:
            indent = len(line) - len(line.lstrip())
            if indent >= 4 or line.startswith("    ") or line.startswith("\t"):
                parts = stripped.split(":", 1)
                current_env["env_vars"][parts[0].strip().lstrip("- ")] = parts[1].strip().strip('"\'')
            else:
                in_env_vars = False
                parts = stripped.split(":", 1)
                current_env[parts[0].strip().lstrip("- ")] = parts[1].strip().strip('"\'')
        elif current_env and ":" in stripped:
            in_env_vars = False
            parts = stripped.split(":", 1)
            key = parts[0].strip().lstrip("- ")
            val = parts[1].strip().strip('"\'')
            if key in ("project_id", "cluster_name", "region", "namespace", "description"):
                current_env[key] = val
    if current_env:
        result["environments"].append(current_env)
    return result


def _get_default_config_env() -> Dict[str, Any]:
    """Retrieves and merges defaults and default environment settings defined in e2e_config.yaml."""
    if not _DEFAULT_CONFIG_PATH.is_file():
        return {}
    content = _DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
    try:
        cfg = yaml.safe_load(content) if yaml else _parse_yaml_fallback(content)
    except Exception:
        cfg = _parse_yaml_fallback(content)

    defaults = (cfg or {}).get("defaults", {})
    envs = (cfg or {}).get("environments", [])
    default_env_name = os.environ.get("E2E_ENV") or defaults.get("default_environment", "investigations-e2e")
    default_env = next((e for e in envs if e.get("name") == default_env_name), {})
    return {
        "env_vars": default_env.get("env_vars", {}),
        **defaults,
        **{k: v for k, v in default_env.items() if k != "env_vars"},
    }


@pytest.fixture(scope="session")
def gcp_project_id() -> Optional[str]:
    """Resolves GCP Project ID from environment or e2e_config.yaml."""
    val = os.environ.get("GCP_PROJECT_ID") or os.environ.get("PROJECT_ID")
    if val:
        return val

    cfg = _get_default_config_env()
    return cfg.get("project_id")


@pytest.fixture(scope="session")
def gke_cluster_name() -> Optional[str]:
    """Resolves GKE Cluster Name from environment or e2e_config.yaml."""
    val = os.environ.get("GKE_CLUSTER_NAME") or os.environ.get("CLUSTER_NAME")
    if val:
        return val

    cfg = _get_default_config_env()
    return cfg.get("cluster_name")


@pytest.fixture(scope="session")
def gcp_region() -> str:
    """Resolves GCP Region from environment or e2e_config.yaml."""
    val = os.environ.get("GCP_REGION") or os.environ.get("REGION")
    if val:
        return val

    cfg = _get_default_config_env()
    return cfg.get("region") or "us-east4"


@pytest.fixture(scope="session")
def agent_namespace() -> str:
    """Resolves Kubernetes namespace where platform-agent is deployed."""
    val = os.environ.get("AGENT_NAMESPACE")
    if val:
        return val
    cfg = _get_default_config_env()
    return cfg.get("namespace") or "kubeagents-system"





@pytest.fixture(scope="session")
def fleet_audit_streams() -> str:
    """Resolves FLEET_AUDIT_STREAMS filter ('all' or specific stream name like 'stockout-prevention')."""
    val = os.environ.get("FLEET_AUDIT_STREAMS")
    if val:
        return str(val).strip().lower()
    cfg = _get_default_config_env()
    env_vars = cfg.get("env_vars", {})
    return str(env_vars.get("FLEET_AUDIT_STREAMS") or "all").strip().lower()


def _clean(value: Optional[str]) -> Optional[str]:
    """Trims surrounding whitespace, returning None when no name is left."""
    text = str(value).strip() if value else ""
    return text or None


def _clean_owner(value: Optional[str]) -> Optional[str]:
    """_clean for an owner, which carries no slash of its own.

    Kept separate from _clean because stripping slashes off a *repository* would turn a
    half-written 'owner/' into the bare name 'owner' and hide the missing half.
    """
    owner = _clean(value)
    return _clean(owner.strip("/")) if owner else None


def _resolve_github_org() -> Optional[str]:
    """Resolves the GitHub owner from the environment or e2e_config.yaml.

    Deliberately does not consult GITHUB_REPO, unlike the github_org fixture:
    _qualify_repo calls this while it is still deciding what github_repo resolves to.
    """
    return _clean_owner(os.environ.get("GITHUB_ORG")) or _clean_owner(
        _get_default_config_env().get("env_vars", {}).get("GITHUB_ORG")
    )


def _qualify_repo(repo: Optional[str]) -> Optional[str]:
    """Prefixes a bare repository name with the owner.

    GH_REPO is bare by repository convention -- reusable-deploy-integrations.yml passes
    the org and the repo to the GitHub Token Minter as separate values -- while every
    consumer of this fixture wants 'owner/repo': test_github_target_repository_configuration
    asserts the shape, and github_token_refresh.py rejects anything else.

    The owner comes from GITHUB_ORG or, failing that, e2e_config.yaml, which hard-codes
    it for every e2e environment. So in CI a bare name is always composed, and the
    owner it gets is that config default whenever nothing more specific is set.

    Only a value with no slash in it is composed. Anything else is returned as the
    caller gave it, trimmed of surrounding whitespace: 'owner/' and '/repo' stay as they
    are and fail the caller's structure check naming the value, rather than being
    reshaped into a repository that parses and does not exist. Whitespace alone
    resolves to None for the same reason -- 'org/  ' passes both halves of that check.
    """
    repo = _clean(repo)
    if not repo or "/" in repo:
        return repo
    org = _resolve_github_org()
    return f"{org}/{repo}" if org else repo


@pytest.fixture(scope="session")
def github_repo(agent_namespace: str) -> Optional[str]:
    """Resolves the registered GitOps/Audit repository (owner/repo)."""
    val = os.environ.get("GITHUB_REPO") or os.environ.get("GITOPS_REPO")
    if val:
        return _qualify_repo(val)

    # Try reading from platform-agent-settings in the cluster
    try:
        cmd = [
            "kubectl", "get", "cm", "platform-agent-settings",
            "-n", agent_namespace,
            "-o", "jsonpath={.data.SETTINGS\\.md}",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "Git Repo:" in line:
                    repo_cand = line.split("Git Repo:", 1)[1].strip().strip("*` ")
                    if repo_cand and repo_cand.lower() != "none":
                        return _qualify_repo(repo_cand)
    except Exception:
        pass

    cfg = _get_default_config_env()
    env_vars = cfg.get("env_vars", {})
    return _qualify_repo(env_vars.get("GITHUB_REPO") or env_vars.get("GITOPS_REPO"))


@pytest.fixture(scope="session")
def github_org(github_repo: Optional[str]) -> Optional[str]:
    """Resolves the GitHub Organization name.

    Every branch is normalised the same way github_repo's owner is, so that
    GITHUB_ORG=' myorg' and GITHUB_REPO='myorg/repo' agree. Left un-normalised, the
    two disagree on the whitespace and test_github_target_repository_configuration
    fails on an owner mismatch instead of on the value that caused it.
    """
    val = _clean_owner(os.environ.get("GITHUB_ORG"))
    if val:
        return val
    if github_repo and "/" in github_repo:
        return _clean_owner(github_repo.split("/", 1)[0])
    cfg = _get_default_config_env()
    env_vars = cfg.get("env_vars", {})
    return _clean_owner(env_vars.get("GITHUB_ORG"))


@pytest.fixture(scope="session")
def github_app_id() -> Optional[str]:
    """Resolves the GitHub App ID."""
    val = os.environ.get("GITHUB_APP_ID")
    if val:
        return val
    cfg = _get_default_config_env()
    env_vars = cfg.get("env_vars", {})
    return env_vars.get("GITHUB_APP_ID")


@pytest.fixture(scope="session")
def github_installation_id() -> Optional[str]:
    """Resolves the GitHub App Installation ID."""
    val = os.environ.get("GITHUB_INSTALLATION_ID")
    if val:
        return val
    cfg = _get_default_config_env()
    env_vars = cfg.get("env_vars", {})
    return env_vars.get("GITHUB_INSTALLATION_ID")


@pytest.fixture(scope="session", autouse=True)
def ensure_cluster_credentials(
    gcp_project_id: Optional[str],
    gke_cluster_name: Optional[str],
    gcp_region: str,
) -> None:
    """Configures kubectl context for the target GKE cluster."""
    if gcp_project_id and gke_cluster_name and gcp_region:
        expected_ctx = f"gke_{gcp_project_id}_{gcp_region}_{gke_cluster_name}"
        ctx_res = subprocess.run(["kubectl", "config", "current-context"], capture_output=True, text=True)
        current_ctx = ctx_res.stdout.strip() if ctx_res.returncode == 0 else ""

        check_res = subprocess.run(["kubectl", "cluster-info"], capture_output=True, text=True)
        if check_res.returncode == 0 and (current_ctx == expected_ctx or (gke_cluster_name in current_ctx and gcp_project_id in current_ctx)):
            return

        subprocess.run(
            ["gcloud", "config", "set", "container/use_application_default_credentials", "false", "--quiet"],
            capture_output=True,
        )
        res = subprocess.run(
            [
                "gcloud", "container", "clusters", "get-credentials",
                gke_cluster_name,
                f"--region={gcp_region}",
                f"--project={gcp_project_id}",
            ],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            pytest.fail(f"Failed to get-credentials for cluster '{gke_cluster_name}': {res.stderr}")



@pytest.fixture(scope="session")
def platform_agent_api_key(agent_namespace: str) -> Optional[str]:
    """Retrieves API_SERVER_KEY from platform-agent-secrets in the cluster."""
    try:
        cmd = [
            "kubectl", "get", "secret", "platform-agent-secrets",
            "-n", agent_namespace,
            "-o", "jsonpath={.data.API_SERVER_KEY}",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        raw_key = result.stdout.strip()
        if raw_key:
            return base64.b64decode(raw_key).decode("utf-8")
    except Exception:
        pass
    return os.environ.get("PLATFORM_AGENT_TOKEN") or os.environ.get("API_SERVER_KEY") or "cluster-internal-trusted"


@pytest.fixture(scope="session")
def port_forward_agent(
    agent_namespace: str, platform_agent_api_key: Optional[str]
) -> Generator[Optional[str], None, None]:
    """Establishes background port-forward to the platform agent REST API with guaranteed cleanup."""
    if not platform_agent_api_key:
        yield None
        return

    import socket

    port_str = os.environ.get("AGENT_LOCAL_PORT", "8642")
    port = int(port_str)
    url = f"http://127.0.0.1:{port}"

    # Build prioritized candidate targets: services, deployments, pods
    targets = [
        "svc/platform-agent",
        "svc/platform-agent-credential-proxy",
        "deployment/platform-agent-gateway",
    ]

    # Dynamically discover agent deployments or pods in agent_namespace
    try:
        res_dep = subprocess.run(
            ["kubectl", "get", "deployments", "-n", agent_namespace, "-o", "jsonpath={.items[*].metadata.name}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res_dep.returncode == 0:
            for dep in res_dep.stdout.split():
                if "gateway" in dep or "platform" in dep or "agent" in dep:
                    targets.append(f"deployment/{dep}")

        res_pods = subprocess.run(
            [
                "kubectl", "get", "pods", "-n", agent_namespace,
                "-l", "kubeagents.x-k8s.io/has-credential-proxy=true",
                "-o", "jsonpath={.items[*].metadata.name}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res_pods.returncode == 0:
            for p in res_pods.stdout.split():
                targets.append(f"pod/{p}")
    except Exception:
        pass

    # Deduplicate targets preserving order
    seen = set()
    unique_targets = []
    for t in targets:
        if t not in seen:
            seen.add(t)
            unique_targets.append(t)

    proc = None
    connected = False

    for target in unique_targets:
        proc = subprocess.Popen(
            [
                "kubectl",
                "port-forward",
                target,
                "-n",
                agent_namespace,
                f"{port}:8642",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        deadline = time.time() + 8
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    connected = True
                    break
            except (OSError, ConnectionRefusedError):
                time.sleep(0.3)

        if connected:
            break

        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except Exception:
                proc.kill()
        proc = None

    if not connected or proc is None:
        yield None
        return

    try:
        yield url
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
