#!/usr/bin/env python3
# platform_mcp_server.py - Unified GKE Platform Control Plane MCP Server.
# Exposes secure cross-cluster A2A communication, dynamic GKE IPAM, and declarative cluster provisioning as native tools.

import json
import os
import sys
import urllib.request
import urllib.error
import subprocess
import ipaddress
import tempfile
from pathlib import Path
from datetime import datetime
from mcp.server.fastmcp import FastMCP

# Initialize the FastMCP server
mcp = FastMCP("GKE Platform Control Plane")

def log(msg: str):
    print(f"[PLATFORM-MCP-SERVER] {msg}", file=sys.stderr)


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
            capture_output=True, text=True, check=True
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
            capture_output=True, text=True, check=True
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
        ["kubectl", "delete", "containercluster", cluster_name, "-n", "agent-system", "--wait=false"],
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
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
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
            capture_output=True, text=True, check=True
        )
        return f"SUCCESS: Notification posted to Google Chat. Output: {res.stdout.strip()}"
    except subprocess.CalledProcessError as e:
        return f"ERROR: Failed to send notification: {e.stderr.strip()}"
    except Exception as e:
        return f"ERROR: {e}"

if __name__ == "__main__":
    mcp.run()
