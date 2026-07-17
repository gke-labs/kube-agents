#!/usr/bin/env python3
# platform_mcp_server.py - Unified GKE Platform Control Plane MCP Server.
# Exposes secure cross-cluster A2A communication, dynamic GKE IPAM, and declarative cluster provisioning as native tools.

import json
import os
import re
import socket
import sys
import urllib.request
import urllib.error
import subprocess
import ipaddress
import tempfile
from pathlib import Path
from datetime import datetime
from mcp.server.fastmcp import FastMCP

DEFAULT_SESSION_KV_DB_PATH = "/var/lib/kube-agents/session/session_kv.db"

# Initialize the FastMCP server
mcp = FastMCP("GKE Platform Control Plane")

def log(msg: str):
    print(f"[PLATFORM-MCP-SERVER] {msg}", file=sys.stderr)


def _run_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a subprocess env with HOME redirected to /tmp so gcloud/kubectl write credentials to the writable scratch disk inside non-root container pods."""
    return {**os.environ, "HOME": "/tmp", **(extra or {})}


def _strip_kubectl_noise(stdout: str) -> str:
    """Drop high-volume, low-signal fields from `kubectl get -o json` output before returning to the LLM."""
    try:
        obj = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return stdout
    for item in obj.get("items", [obj]):
        meta = item.get("metadata", {})
        for k in ("managedFields", "resourceVersion", "uid", "generation", "creationTimestamp"):
            meta.pop(k, None)
    return json.dumps(obj, indent=2)


def _pod_summary(pod: dict) -> dict | None:
    """Summarize a Pod object as {name, status, restarts}. Reports every non-empty container reason (labeled by container) so multi-container failures aren't hidden by last-write-wins."""
    meta = pod.get("metadata") or {}
    name = meta.get("name")
    if not name:
        return None
    status = pod.get("status") or {}
    all_cs = (status.get("containerStatuses") or []) + (status.get("initContainerStatuses") or [])
    restarts = 0
    reasons = []
    for cs in all_cs:
        restarts += cs.get("restartCount", 0)
        state = cs.get("state") or {}
        r = (state.get("waiting") or {}).get("reason") or (state.get("terminated") or {}).get("reason")
        if r:
            reasons.append(f"{cs.get('name', '?')}={r}")
    return {
        "name": name,
        "status": "; ".join(reasons) if reasons else status.get("phase", "Unknown"),
        "restarts": restarts,
    }


def _strip_audit_log_noise(stdout: str) -> str:
    """Drop high-cardinality/redundant fields from `gcloud logging read --format=json` output before returning to the LLM."""
    try:
        entries = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return stdout
    if not isinstance(entries, list):
        return stdout
    for entry in entries:
        for k in ("insertId", "receiveTimestamp", "logName"):
            entry.pop(k, None)
        pp = entry.get("protoPayload")
        if isinstance(pp, dict):
            pp.pop("@type", None)
    return json.dumps(entries, indent=2)


def get_hermes_home() -> Path:
    """Return the active HERMES_HOME directory."""
    return Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))




# =============================================================================
# GCP Region Validation Helpers
# =============================================================================

def get_project_id() -> str:
    """Resolve Project ID from USER.md or gcloud config."""
    user_md = get_hermes_home() / "USER.md"
    if user_md.exists():
        try:
            content = user_md.read_text(encoding="utf-8")
            for line in content.splitlines():
                if "project:" in line.lower():
                    val = line.split(":", 1)[1].strip().strip('"').strip("'")
                    if val:
                        return val
        except Exception as e:
            log(f"Warning: Failed to parse USER.md: {e}")

    try:
        res = subprocess.run(
            ["gcloud", "config", "get-value", "project"],
            capture_output=True, text=True, check=True, env=_run_env()
        )
        val = res.stdout.strip()
        if val and val != "(unset)":
            return val
    except Exception as e:
        log(f"Warning: Failed to query gcloud config: {e}")

    return ""


def get_valid_regions(project_id: str) -> list[str]:
    """Retrieve the live list of enabled Google Cloud regions for the GKE API."""
    try:
        res = subprocess.run(
            [
                "gcloud", "compute", "regions", "list",
                f"--project={project_id}",
                "--format=value(name)"
            ],
            capture_output=True, text=True, check=True, env=_run_env()
        )
        regions = [line.strip() for line in res.stdout.splitlines() if line.strip()]
        if regions:
            return regions
    except Exception as e:
        log(f"Warning: Failed to query live GCP regions: {e}. Using SRE fallback list.")

    return [
        "us-central1", "us-east1", "us-east4", "us-west1", "us-west2",
        "europe-west1", "europe-west2", "europe-west3", "europe-west4",
        "asia-east1", "asia-east2", "asia-northeast1", "asia-northeast2"
    ]


def validate_location(location: str, project_id: str) -> str:
    """Verify GKE location. Return error message on failure, empty string on success."""
    valid_regions = get_valid_regions(project_id)
    region_base = "-".join(location.split("-")[:2])

    if location not in valid_regions and region_base not in valid_regions:
        err = f"ERROR: Invalid GKE location '{location}' specified.\nPossible valid GKE regions in your project:\n"
        for r in sorted(valid_regions):
            err += f"  - {r}\n"
        return err.strip()
    return ""


# =============================================================================
# GKE Declarative Apply / Delete Helpers
# =============================================================================

def apply_manifest(path: str):
    """Execute kubectl apply on the manifest path using secure in-cluster token."""
    subprocess.run(
        ["kubectl", "apply", "-f", path],
        check=True, capture_output=True, text=True
    )


def delete_cluster_manifest(cluster_name: str):
    """Delete the GKE cluster Custom Resource from the namespace asynchronously."""
    subprocess.run(
        ["kubectl", "delete", "containercluster", cluster_name, "-n", "kubeagents-system", "--wait=false"],
        check=True, capture_output=True, text=True
    )


@mcp.tool()
def verify_gke_cluster(cluster_name: str, location: str, project_id: str = "") -> str:
    """
    Verify the existence and current status of a GKE cluster in Google Cloud.
    Returns JSON string with 'exists' flag and status if running.

    Args:
        cluster_name: The name of the GKE cluster.
        location: The GCP region or zone (e.g. 'us-central1' or 'us-central1-a').
        project_id: Optional GCP Project ID. If omitted, resolves automatically.
    """
    pid = project_id if project_id else get_project_id()
    if not pid:
        return "ERROR: Could not resolve GCP Project ID. Please specify 'project_id'."

    err = validate_location(location, pid)
    if err:
        return err

    cmd = [
        "gcloud", "container", "clusters", "describe", cluster_name,
        f"--location={location}",
        f"--project={pid}",
        "--format=json(status, id)"
    ]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True, env=_run_env())
        data = json.loads(res.stdout)
        return json.dumps({
            "exists": True,
            "status": data.get("status"),
            "id": data.get("id")
        }, indent=2)
    except subprocess.CalledProcessError as e:
        if "NotFound" in e.stderr or "not found" in e.stderr.lower() or "404" in e.stderr:
            return json.dumps({
                "exists": False
            }, indent=2)
        return f"ERROR: Failed to describe GKE cluster.\nExit Code: {e.returncode}\nStderr: {e.stderr}"
    except Exception as e:
        return f"ERROR: An unexpected error occurred: {e}"


def switch_kube_context(project_id: str, cluster_name: str, location: str) -> tuple[str, dict[str, str]]:
    """
    Point kubectl to the target GKE cluster using a thread-isolated kubeconfig.
    Returns (error_string, env_dict). If error_string is non-empty, switching failed.
    env_dict is always populated (with HOME=/tmp injected) and should be passed as
    env=env_dict to subsequent subprocess.run calls.
    """
    if not project_id and not cluster_name and not location:
        return "", _run_env()
    if not project_id or not cluster_name or not location:
        return (
            "ERROR: Target cluster context partially specified. When specifying a"
            " cluster context, all three parameters ('project_id', 'cluster_name',"
            " and 'location') must be provided to avoid querying the wrong"
            " cluster.",
            _run_env(),
        )

    kubeconfig_path = f"/tmp/kubeconfig_{project_id}_{cluster_name}_{location}.yaml"
    env = _run_env({"KUBECONFIG": kubeconfig_path})

    cmd = [
        "gcloud", "container", "clusters", "get-credentials", cluster_name,
        f"--location={location}",
        f"--project={project_id}"
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30, env=env)
        return "", env
    except subprocess.CalledProcessError as e:
        return (
            f"ERROR: Failed to switch kube context to cluster '{cluster_name}'.\nExit Code: {e.returncode}\nStderr: {e.stderr}",
            env,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: Timed out switching kube context to cluster '{cluster_name}'.", env


@mcp.tool()
def list_cc_healthchecks(project_id: str = "", cluster_name: str = "", location: str = "") -> str:
    """
    List the status of Config Controller health checks on the management cluster.
    Provides diagnostic information on failed host-level health synchronizations.

    Args:
        project_id: Optional GCP Project ID context.
        cluster_name: Optional target cluster name context.
        location: Optional GKE location context.
    """
    cmd = [
        "kubectl", "get", "healthchecks.healthcheck.config.gke.io",
        "-n", "krmapihosting-system",
        "-o", "json"
    ]

    try:
        ctx_err, env = switch_kube_context(project_id, cluster_name, location)
        if ctx_err:
            return ctx_err
        res = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30, env=env)
        return _strip_kubectl_noise(res.stdout)
    except subprocess.TimeoutExpired:
        return "ERROR: Timed out querying Config Controller health checks after 30 seconds."
    except subprocess.CalledProcessError as e:
        return f"ERROR: Failed to query Config Controller health checks.\nExit Code: {e.returncode}\nStderr: {e.stderr}"
    except Exception as e:
        return f"ERROR: An unexpected error occurred: {e}"


@mcp.tool()
def get_cc_operator_status(project_id: str = "", cluster_name: str = "", location: str = "") -> str:
    """
    Retrieve the status of GKE Config Connector operator resource to diagnose health issues.

    Args:
        project_id: Optional GCP Project ID context.
        cluster_name: Optional target cluster name context.
        location: Optional GKE location context.
    """
    cmd = [
        "kubectl", "get", "configconnectors.core.cnrm.cloud.google.com",
        "-o", "json"
    ]

    try:
        ctx_err, env = switch_kube_context(project_id, cluster_name, location)
        if ctx_err:
            return ctx_err
        res = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30, env=env)
        return _strip_kubectl_noise(res.stdout)
    except subprocess.TimeoutExpired:
        return "ERROR: Timed out retrieving Config Controller operator status after 30 seconds."
    except subprocess.CalledProcessError as e:
        return f"ERROR: Failed to retrieve Config Controller operator status.\nExit Code: {e.returncode}\nStderr: {e.stderr}"
    except Exception as e:
        return f"ERROR: An unexpected error occurred: {e}"


@mcp.tool()
def get_cc_pod_diagnostics(
    pod_name: str, project_id: str = "", cluster_name: str = "", location: str = ""
) -> str:
    """
    Execute read-only diagnostic checks (status JSON, describe, current logs, and previous crash logs)
    on a specific system pod inside the Config Controller management cluster (`krmapihosting-system`).

    Args:
        pod_name: The target pod name to diagnose (e.g., 'bootstrap-pod-xyz', 'git-sync-pod-abc').
        project_id: Optional GCP Project ID context.
        cluster_name: Optional target cluster name context.
        location: Optional GKE location context.
    """
    if not pod_name or not re.match(r"^[a-z0-9.-]+$", pod_name):
        return f"ERROR: Invalid pod name format '{pod_name}'. Pod names must contain only lowercase alphanumeric characters, dots, and hyphens."

    ns = "krmapihosting-system"
    describe_cmd = ["kubectl", "describe", "pod", pod_name, "-n", ns]
    logs_cmd = ["kubectl", "logs", pod_name, "-n", ns, "--all-containers", "--tail=100"]
    prev_logs_cmd = ["kubectl", "logs", pod_name, "-n", ns, "--all-containers", "--previous", "--tail=100"]

    results = []

    ctx_err, env = switch_kube_context(project_id, cluster_name, location)
    if ctx_err:
        return ctx_err

    try:
        res = subprocess.run(describe_cmd, capture_output=True, text=True, check=True, timeout=30, env=env)
        results.append(f"=== POD DESCRIBE ===\n{res.stdout}\n")
    except subprocess.TimeoutExpired:
        results.append("=== POD DESCRIBE TIMEOUT ===\nCommand timed out after 30 seconds.\n")
    except subprocess.CalledProcessError as e:
        results.append(f"=== POD DESCRIBE ERROR ===\nExit Code: {e.returncode}\nStderr: {e.stderr}\n")

    try:
        res = subprocess.run(logs_cmd, capture_output=True, text=True, check=True, timeout=30, env=env)
        results.append(f"=== POD LOGS (CURRENT TAIL=100) ===\n{res.stdout}\n")
    except subprocess.TimeoutExpired:
        results.append("=== POD LOGS (CURRENT TAIL=100) TIMEOUT ===\nCommand timed out after 30 seconds.\n")
    except subprocess.CalledProcessError as e:
        results.append(f"=== POD LOGS (CURRENT TAIL=100) ERROR ===\nExit Code: {e.returncode}\nStderr: {e.stderr}\n")

    try:
        res = subprocess.run(prev_logs_cmd, capture_output=True, text=True, check=True, timeout=30, env=env)
        results.append(f"=== POD LOGS (PREVIOUS TAIL=100) ===\n{res.stdout}\n")
    except subprocess.TimeoutExpired:
        results.append("=== POD LOGS (PREVIOUS TAIL=100) TIMEOUT ===\nCommand timed out after 30 seconds.\n")
    except subprocess.CalledProcessError as e:
        results.append(f"=== POD LOGS (PREVIOUS TAIL=100) ===\nNo previous container logs available (container has not restarted or previous logs expired).\n")

    return "\n".join(results)


@mcp.tool()
def list_cc_pods(project_id: str = "", cluster_name: str = "", location: str = "") -> str:
    """
    List the names and statuses of critical Config Connector and Config Controller system pods
    in the management cluster's hosting namespace.

    Args:
        project_id: Optional GCP Project ID context.
        cluster_name: Optional target cluster name context.
        location: Optional GKE location context.
    """
    cmd = [
        "kubectl", "get", "pods",
        "-n", "krmapihosting-system",
        "-o", "json"
    ]

    try:
        ctx_err, env = switch_kube_context(project_id, cluster_name, location)
        if ctx_err:
            return ctx_err
        res = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30, env=env)
        data = json.loads(res.stdout)
        pods = [s for s in (_pod_summary(p) for p in (data.get("items") or [])) if s]
        return json.dumps(pods, indent=2)
    except subprocess.TimeoutExpired:
        return "ERROR: Timed out listing Config Controller pods after 30 seconds."
    except subprocess.CalledProcessError as e:
        return f"ERROR: Failed to list Config Controller pods.\nExit Code: {e.returncode}\nStderr: {e.stderr}"
    except Exception as e:
        return f"ERROR: An unexpected error occurred: {e}"


@mcp.tool()
def audit_log_searcher(project_id: str = "", cluster_name: str = "", location: str = "") -> str:
    """
    Search Google Cloud Audit Logs to check if the GKE bootstrap deployment
    or related resources were manually deleted by a user.

    Args:
        project_id: Optional GCP Project ID. If omitted, resolves automatically.
        cluster_name: Optional target GKE cluster name.
        location: Optional GKE location context.
    """
    pid = project_id if project_id else get_project_id()
    if not pid:
        return "ERROR: Could not resolve GCP Project ID. Please specify 'project_id'."

    filters = [
        '(resource.type="k8s_cluster" OR resource.type="gke_cluster")',
        'protoPayload.methodName:delete',
        '"deployments/bootstrap"'
    ]
    if cluster_name:
        filters.append(f'resource.labels.cluster_name="{cluster_name}"')
    if location:
        filters.append(f'resource.labels.location="{location}"')

    filter_expr = " AND ".join(filters)

    cmd = [
        "gcloud", "logging", "read",
        filter_expr,
        f"--project={pid}",
        "--freshness=7d",
        "--limit=5",
        "--format=json"
    ]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30, env=_run_env())
        return _strip_audit_log_noise(res.stdout)
    except subprocess.TimeoutExpired:
        return "ERROR: Cloud Audit Logs query timed out after 30 seconds."
    except subprocess.CalledProcessError as e:
        return f"ERROR: Failed to query Cloud Audit Logs.\nExit Code: {e.returncode}\nStderr: {e.stderr}"
    except Exception as e:
        return f"ERROR: An unexpected error occurred: {e}"


@mcp.tool()
def send_notification(message: str) -> str:
    """
    Post a formatted alert or operational notification directly to the user's primary Google Chat home channel.

    Args:
        message: The plaintext or markdown-formatted message string to post.
    """
    try:
        res = subprocess.run(
            ["hermes", "send", "--to", "google_chat", message],
            capture_output=True, text=True, check=True, env=_run_env()
        )
        return f"SUCCESS: Notification posted to Google Chat. Output: {res.stdout.strip()}"
    except subprocess.CalledProcessError as e:
        return f"ERROR: Failed to send notification: {e.stderr.strip()}"
    except Exception as e:
        return f"ERROR: {e}"


def start_session_kv_server() -> None:
    """Start the session metadata HTTP resolver when the MCP server starts."""
    try:
        port = 8699
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                log(f"Session KV server is already running on port {port}.")
                return

        app_dir = Path(__file__).resolve().parent.parent
        log(f"Starting Session KV server on port {port}.")
        subprocess.Popen(
            [
                "/opt/hermes/.venv/bin/python3",
                "-m",
                "uvicorn",
                "scripts.session_kv_server:app",
                "--app-dir",
                str(app_dir),
                "--host",
                "0.0.0.0",
                "--port",
                str(port),
            ],
            cwd=str(app_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env={
                **os.environ,
                "SESSION_KV_DB_PATH": os.environ.get("SESSION_KV_DB_PATH", DEFAULT_SESSION_KV_DB_PATH),
            },
        )
        log("Session KV server spawned successfully.")
    except Exception as exc:
        log(f"Failed to start Session KV server: {exc}")


if __name__ == "__main__":
    start_session_kv_server()
    mcp.run()

