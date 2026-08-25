#!/usr/bin/env python3
"""Declarative E2E Test Suite Runner for kube-agents.

Reads the test matrix from tests/e2e/e2e_config.yaml (or $E2E_CONFIG),
resolves target GCP project, GKE cluster, region, and namespace for each
environment (e.g. gchat-e2e, agent-plugin-e2e), and executes the specified
pytest suites. The cluster is expected to exist already; nothing here
provisions infrastructure.
"""

import argparse
import json
import os
import pathlib
import subprocess
import sys
from typing import Any, Dict, List, Optional

if "CLOUDSDK_PYTHON" in os.environ:
    del os.environ["CLOUDSDK_PYTHON"]
if "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE" in os.environ:
    del os.environ["CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"]
os.environ["CLOUDSDK_PYTHON_SITEPACKAGES"] = "0"
os.environ["PYTHONNOUSERSITE"] = "1"
os.environ["USE_GKE_GCLOUD_AUTH_PLUGIN"] = "True"
os.environ["CLOUDSDK_CONTAINER_USE_APPLICATION_DEFAULT_CREDENTIALS"] = "false"

if "GOOGLE_APPLICATION_CREDENTIALS" in os.environ and os.path.isfile(os.environ["GOOGLE_APPLICATION_CREDENTIALS"]):
    subprocess.run(
        ["gcloud", "auth", "activate-service-account", f"--key-file={os.environ['GOOGLE_APPLICATION_CREDENTIALS']}", "--quiet"],
        capture_output=True,
    )

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_PATH = _REPO_ROOT / "tests" / "e2e" / "e2e_config.yaml"

try:
    from dotenv import load_dotenv
    _env_file = _REPO_ROOT / ".env"
    if _env_file.is_file():
        load_dotenv(_env_file)
except ImportError:
    pass


def load_yaml_config(config_path: pathlib.Path) -> Dict[str, Any]:
    """Loads YAML configuration with PyYAML or a fallback parser."""
    if not config_path.is_file():
        print(f"Error: Configuration file not found at {config_path}", file=sys.stderr)
        sys.exit(1)

    content = config_path.read_text(encoding="utf-8")
    try:
        import yaml
        return yaml.safe_load(content) or {}
    except ImportError:
        # Robust fallback parser for simple environments list and nested env_vars
        cfg: Dict[str, Any] = {"defaults": {}, "environments": []}
        current_env: Optional[Dict[str, Any]] = None
        in_env_vars = False
        in_tests = False
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("- name:"):
                in_env_vars = False
                in_tests = False
                if current_env:
                    cfg["environments"].append(current_env)
                name = stripped.split(":", 1)[1].strip().strip('"\'')
                current_env = {
                    "name": name,
                    "tests": [],
                    "env_vars": {},
                }
            elif current_env and stripped.startswith("tests:"):
                in_tests = True
                in_env_vars = False
            elif current_env and in_tests and stripped.startswith("- "):
                current_env["tests"].append(stripped.lstrip("- ").strip().strip('"\''))
            elif current_env and stripped.startswith("- ") and "tests/" in stripped:
                in_env_vars = False
                current_env["tests"].append(stripped.lstrip("- ").strip().strip('"\''))
            elif current_env and stripped.startswith("env_vars:"):
                in_env_vars = True
                in_tests = False
            elif current_env and in_env_vars and ":" in stripped:
                indent = len(line) - len(line.lstrip())
                if indent >= 4 or line.startswith("    ") or line.startswith("\t"):
                    k, v = stripped.split(":", 1)
                    current_env["env_vars"][k.strip().lstrip("- ")] = v.strip().strip('"\'')
                else:
                    in_env_vars = False
                    k, v = stripped.split(":", 1)
                    if k.strip() != "tests":
                        current_env[k.strip().lstrip("- ")] = v.strip().strip('"\'')
            elif current_env and ":" in stripped:
                in_env_vars = False
                k, v = stripped.split(":", 1)
                if k.strip() != "tests":
                    current_env[k.strip().lstrip("- ")] = v.strip().strip('"\'')
        if current_env:
            cfg["environments"].append(current_env)
        return cfg


def connect_gke_credentials(project_id: str, cluster_name: str, region: str) -> None:
    """Configures kubectl context for target GKE cluster and verifies API server connectivity."""
    expected_ctx = f"gke_{project_id}_{region}_{cluster_name}"
    ctx_res = subprocess.run(["kubectl", "config", "current-context"], capture_output=True, text=True)
    current_ctx = ctx_res.stdout.strip() if ctx_res.returncode == 0 else ""

    info_res = subprocess.run(["kubectl", "cluster-info"], capture_output=True, text=True)
    if info_res.returncode == 0 and (current_ctx == expected_ctx or (cluster_name in current_ctx and project_id in current_ctx)):
        print(f"✓ Using established kubectl cluster connection for '{cluster_name}' ({current_ctx}).")
        return

    subprocess.run(
        ["gcloud", "config", "set", "container/use_application_default_credentials", "false", "--quiet"],
        capture_output=True,
    )
    cmd = [
        "gcloud",
        "container",
        "clusters",
        "get-credentials",
        cluster_name,
        f"--region={region}",
        f"--project={project_id}",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(
            f"Error: Failed to connect kubectl to GKE cluster '{cluster_name}': {res.stderr.strip()}",
            file=sys.stderr,
        )
        sys.exit(1)

    info_res = subprocess.run(["kubectl", "cluster-info"], capture_output=True, text=True)
    if info_res.returncode != 0:
        print(
            f"Error: Kubectl cannot communicate with cluster API server for '{cluster_name}': {info_res.stderr.strip()}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"✓ Connected kubectl context to cluster '{cluster_name}' in '{region}'.")


def find_pytest_executable() -> str:
    """Locates the pytest binary, prioritizing the repo virtualenv."""
    venv_pytest = _REPO_ROOT / "bench" / ".venv" / "bin" / "pytest"
    if venv_pytest.is_file() and os.access(venv_pytest, os.X_OK):
        return str(venv_pytest)
    return "pytest"


def run_environment_tests(
    env: Dict[str, Any],
    defaults: Dict[str, Any],
    extra_args: List[str],
) -> int:
    """Executes pytest for a single environment definition."""
    project_id = os.environ.get("GCP_PROJECT_ID") or os.environ.get("PROJECT_ID") or env.get("project_id", "")
    cluster_name = os.environ.get("GKE_CLUSTER_NAME") or os.environ.get("CLUSTER_NAME") or env.get("cluster_name", "")
    region = os.environ.get("GCP_REGION") or os.environ.get("REGION") or env.get("region") or defaults.get("region", "us-east4")
    namespace = os.environ.get("AGENT_NAMESPACE") or env.get("namespace") or defaults.get("namespace", "kubeagents-system")
    tests = env.get("tests") or ["tests/e2e/"]

    env_name = env.get("name", "default")
    print("\n" + "=" * 60)
    print(f"Executing E2E Suite: {env_name}")
    print(f"Project:    {project_id}")
    print(f"Cluster:    {cluster_name} ({region})")
    print(f"Namespace:  {namespace}")
    print(f"Tests:      {', '.join(tests)}")
    print("=" * 60 + "\n")

    # Cluster-backed environments must have valid project_id and cluster_name
    if env_name != "gchat-e2e":
        if not project_id or not cluster_name:
            print(
                f"Error: E2E environment '{env_name}' requires GCP_PROJECT_ID and GKE_CLUSTER_NAME environment variables.",
                file=sys.stderr,
            )
            return 1
        connect_gke_credentials(project_id, cluster_name, region)
    elif project_id and cluster_name:
        connect_gke_credentials(project_id, cluster_name, region)

    # Merge custom environment variables: YAML defaults must not override explicit workflow environment
    custom_env_vars = env.get("env_vars", {})
    kube_ctx = os.environ.get("KUBE_CONTEXT") or (f"gke_{project_id}_{region}_{cluster_name}" if (project_id and cluster_name and region) else "")
    reg = os.environ.get("REGISTRY") or os.environ.get("REGISTRY_PREFIX") or (f"{region}-docker.pkg.dev/{project_id}/kube-agents" if (project_id and region) else "")
    env_vars = {
        **custom_env_vars,
        **os.environ,
        "USE_GKE_GCLOUD_AUTH_PLUGIN": "True",
        "CLOUDSDK_PYTHON_SITEPACKAGES": "0",
        "PYTHONNOUSERSITE": "1",
        "CLOUDSDK_CONTAINER_USE_APPLICATION_DEFAULT_CREDENTIALS": "false",
        "PATH": f"{pathlib.Path.home()}/.local/bin:{os.environ.get('PATH', '')}",
        "GCP_PROJECT_ID": project_id,
        "GKE_CLUSTER_NAME": cluster_name,
        "GCP_REGION": region,
        "AGENT_NAMESPACE": namespace,
        "KUBE_CONTEXT": kube_ctx,
        "REGISTRY": reg,
        # Name the environment we picked, so conftest's fixtures fall through to this
        # environment's env_vars rather than e2e_config.yaml's default_environment.
        # No fixture changes value today -- every key a config lookup would reach is
        # either exported above or already in os.environ from **custom_env_vars -- with
        # one exception. e2e-nightly-matrix.yml exports E2E_ENV from a dispatch input
        # whose choices include "all"; this loop expands that into one child per
        # environment, but the ambient "all" used to ride through to every one of them,
        # and conftest matches names exactly, so the lookup found nothing. Naming the
        # child's own environment also stops being cosmetic the moment a block declares
        # its own project_id or cluster_name.
        "E2E_ENV": env_name,
    }
    if "CLOUDSDK_PYTHON" in env_vars:
        del env_vars["CLOUDSDK_PYTHON"]

    pytest_bin = find_pytest_executable()
    cmd = [pytest_bin] + tests + ["-v", "-s"] + extra_args

    proc = subprocess.run(cmd, env=env_vars, cwd=str(_REPO_ROOT))
    return proc.returncode


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Execute E2E test suites configured in tests/e2e/e2e_config.yaml"
    )
    parser.add_argument(
        "--config",
        type=pathlib.Path,
        default=pathlib.Path(os.environ.get("E2E_CONFIG", _DEFAULT_CONFIG_PATH)),
        help="Path to YAML test matrix configuration file",
    )
    parser.add_argument(
        "--env",
        type=str,
        help="Filter execution to a specific environment name (e.g. gchat-e2e, agent-plugin-e2e)",
    )

    args, extra_args = parser.parse_known_args()

    config = load_yaml_config(args.config)
    defaults = config.get("defaults", {})
    environments: List[Dict[str, Any]] = config.get("environments", [])

    if not environments:
        print("Warning: No environments defined. Running pytest tests/e2e/ directly.")
        pytest_bin = find_pytest_executable()
        sys.exit(
            subprocess.run(
                [pytest_bin, "tests/e2e/", "-v", "-s"] + extra_args,
                cwd=str(_REPO_ROOT),
            ).returncode
        )

    selected_env = args.env or os.environ.get("E2E_ENV") or defaults.get("default_environment", "investigations-e2e")
    # Filter target environments
    if selected_env and selected_env.lower() != "all":
        target_envs = [e for e in environments if e.get("name") == selected_env]
        if not target_envs:
            print(f"Error: Environment '{selected_env}' not found in {args.config}", file=sys.stderr)
            sys.exit(1)
    else:
        target_envs = environments

    overall_exit_code = 0
    for env in target_envs:
        exit_code = run_environment_tests(
            env,
            defaults,
            extra_args,
        )
        if exit_code != 0:
            overall_exit_code = exit_code

    sys.exit(overall_exit_code)


if __name__ == "__main__":
    main()

