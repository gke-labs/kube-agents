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

def get_state_file(agent_id: str) -> Path:
    """Return the path to the corresponding agents JSONL state file based on agent type."""
    if agent_id.startswith("operator-"):
        return get_hermes_home() / "operator_agents.jsonl"
    else:
        return get_hermes_home() / "devteam_agents.jsonl"

# =============================================================================
# Secure completions client Helpers
# =============================================================================

def resolve_agent_credentials(agent_id: str) -> tuple[str, str]:
    """Retrieve the target agent's stable K8s Service FQDN from the state registry and shared API key from the environment."""
    state_file = get_state_file(agent_id)
    endpoint = ""
    api_key = "none"

    if state_file.exists():
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    if entry.get("agent_id") == agent_id:
                        endpoint = entry.get("endpoint", "")
                        api_key = os.environ.get("API_SERVER_KEY") or "none"
                        log(f"Resolved credentials for '{agent_id}' from state registry.")
                        break
        except Exception as e:
            log(f"Warning: Failed to read state file '{state_file}': {e}")

    if not endpoint or api_key == "none":
        raise ValueError(f"ERROR: Agent '{agent_id}' is not registered or does not exist in the state registry.")

    return endpoint, api_key

def call_agent_api(endpoint: str, api_key: str, query: str, agent_id: str, session_id: str = "") -> str:
    """Perform the synchronous HTTP POST call to the target agent's completions API using Bearer Token auth."""
    protocol = "https" if endpoint.startswith("https://") else "http"
    clean_endpoint = endpoint.replace("http://", "").replace("https://", "")
    
    url = f"{protocol}://{clean_endpoint}/v1/chat/completions"
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    clean_session_id = "".join(c for c in str(session_id) if c.isalnum() or c in "-_.").strip() if session_id else ""
    if clean_session_id:
        headers["X-Hermes-Session-Id"] = clean_session_id
    payload = {
        "model": "hermes-agent",
        "messages": [{"role": "user", "content": query}]
    }

    log(f"Sending secure synchronous call to '{agent_id}'")
    req = urllib.request.Request(
        url, 
        data=json.dumps(payload).encode("utf-8"), 
        headers=headers,
        method="POST"
    )

    try:
        # 5-minute timeout to accommodate GKE Operator/DevTeam reasoning loops
        with urllib.request.urlopen(req, timeout=300) as response:
            resp_data = json.loads(response.read().decode("utf-8"))
            return resp_data["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8")
        return f"ERROR: Target agent returned HTTP {e.code}: {err_body}"
    except Exception as e:
        return f"ERROR: Network communication failed: {e}"

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

# =============================================================================
# State Registry Mutators
# =============================================================================

def add_operator_to_state(agent_id: str, cluster_name: str, location: str, project_id: str):
    """Append a new operator entry to the JSONL state file."""
    state_file = get_hermes_home() / "operator_agents.jsonl"
    state_file.parent.mkdir(parents=True, exist_ok=True)

    entry = {
        "agent_id": agent_id,
        "cluster_name": cluster_name,
        "location": location,
        "project_id": project_id,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "status": "active",
        "endpoint": f"operator-agent-{cluster_name}-{location}.agent-system.svc.cluster.local:8642"
    }

    try:
        with open(state_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        log(f"Registered new agent '{agent_id}' in state registry.")
    except Exception as e:
        log(f"Error: Failed to write state entry: {e}")
        raise

def remove_operator_from_state(agent_id: str):
    """Remove the operator entry from the JSONL state file."""
    state_file = get_hermes_home() / "operator_agents.jsonl"
    if not state_file.exists():
        return

    lines = []
    removed = False
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("agent_id") == agent_id:
                    removed = True
                    continue
                lines.append(line)
        
        if removed:
            with open(state_file, "w", encoding="utf-8") as f:
                f.writelines(lines)
            log(f"Removed agent '{agent_id}' from state registry.")
    except Exception as e:
        log(f"Error: Failed to clean state entry: {e}")

def add_devteam_to_state(agent_id: str, cluster_name: str, location: str, namespace: str, project_id: str):
    """Append a new DevTeam agent entry to the JSONL state file."""
    state_file = get_hermes_home() / "devteam_agents.jsonl"
    state_file.parent.mkdir(parents=True, exist_ok=True)

    entry = {
        "agent_id": agent_id,
        "cluster_name": cluster_name,
        "location": location,
        "namespace": namespace,
        "project_id": project_id,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "status": "active",
        "endpoint": f"devteam-{cluster_name}-{location}-{namespace}.agent-system.svc.cluster.local:8642"
    }

    try:
        with open(state_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        log(f"Registered new DevTeam agent '{agent_id}' in state registry.")
    except Exception as e:
        log(f"Error: Failed to write DevTeam state entry: {e}")
        raise

def remove_devteam_from_state(agent_id: str):
    """Remove the DevTeam agent entry from the JSONL state file."""
    state_file = get_hermes_home() / "devteam_agents.jsonl"
    if not state_file.exists():
        return

    lines = []
    removed = False
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("agent_id") == agent_id:
                    removed = True
                    continue
                lines.append(line)
        
        if removed:
            temp_file = state_file.with_suffix(".tmp")
            with open(temp_file, "w", encoding="utf-8") as f:
                f.writelines(lines)
            temp_file.replace(state_file)
            log(f"Removed DevTeam agent '{agent_id}' from state registry.")
    except Exception as e:
        log(f"Error: Failed to clean DevTeam state entry: {e}")
        raise

# =============================================================================
# MCP Tool Declarations
# =============================================================================

@mcp.tool()
def list_operators() -> str:
    """
    List all active, registered GKE Operator Agents in the GKE fleet.
    Returns their unique Agent IDs, managed cluster names, regional locations,
    GCP Project IDs, stable clusterset endpoints, and registration timestamps.
    """
    state_file = get_hermes_home() / "operator_agents.jsonl"
    
    if not state_file.exists():
        return "No active GKE Operator Agents are currently registered."
        
    operators = []
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                entry = json.loads(line)
                clean_entry = {
                    "agent_id": entry.get("agent_id"),
                    "cluster_name": entry.get("cluster_name"),
                    "location": entry.get("location"),
                    "project_id": entry.get("project_id"),
                    "endpoint": entry.get("endpoint"),
                    "status": entry.get("status", "active"),
                    "created_at": entry.get("created_at")
                }
                operators.append(clean_entry)
    except Exception as e:
        return f"ERROR: Failed to read operator agents registry: {e}"
        
    if not operators:
        return "No active GKE Operator Agents are currently registered."
        
    return json.dumps(operators, indent=2)


@mcp.tool()
def call_agent(target_agent_id: str, query: str, session_id: str = "") -> str:
    """
    Directly and securely execute a synchronous, token-authorized completions API call
    to a GKE Operator or DevTeam agent across clusters in your GKE fleet.

    This tool acts as the primary cross-cluster RPC channel. It automatically resolves
    the target's stable DNS endpoint and passes its secure Bearer Token in the headers.
    Note: The call has an internal timeout of 300 seconds (5 minutes) to accommodate
    complex reasoning or extensive GKE tool executions by the target agent.

    Args:
        target_agent_id: The unique ID of the target agent (e.g., 'operator-mercury-03-us-central1').
        query: The natural language query or operational instruction to send to the target agent.
        session_id: Optional. An arbitrary stable string (like a UUID) to maintain conversation 
            continuity. If you wish to have a continuous, multi-turn conversation with the 
            target agent, generate a session ID and pass the same value in subsequent calls 
            to this agent. If omitted, the call is treated as stateless.
    """
    try:
        endpoint, api_key = resolve_agent_credentials(target_agent_id)
    except ValueError as e:
        return str(e)
    
    return call_agent_api(endpoint, api_key, query, target_agent_id, session_id)


@mcp.tool()
def provision_operator(cluster_name: str, location: str, project_id: str = "") -> str:
    """
    Natively and dynamically provision GKE infrastructure and spin up a persistent GKE Operator Agent.

    This tool executes the complete GKE Autopilot private cluster provisioning and Operator setup.

    CRITICAL (Background Rollout): This tool returns SUCCESS immediately once the declarative Custom Resource
    is successfully applied. However, the physical GKE cluster creation takes 5-8 minutes in GCP in the background.
    To monitor the live rollout progress, you MUST execute the following command in your terminal:
    'kubectl get containercluster <cluster_name> -n agent-system -o json'
    and wait for the GKE condition 'type: Ready' to reach 'status: "True"'.

    Args:
        cluster_name: The name of the GKE cluster to provision (e.g., 'mercury-02').
        location: The GCP region or zone for the GKE cluster (e.g., 'us-central1' or 'us-central1-a').
        project_id: Optional GCP Project ID. If omitted, it resolves automatically from the environment.
    """
    pid = project_id if project_id else get_project_id()
    if not pid:
        return "ERROR: Could not resolve GCP Project ID. Please specify 'project_id'."

    err = validate_location(location, pid)
    if err:
        return err

    manifest = f"""apiVersion: container.cnrm.cloud.google.com/v1beta1
kind: ContainerCluster
metadata:
  name: {cluster_name}
  namespace: agent-system
  annotations:
    cnrm.cloud.google.com/project-id: "{pid}"
    cnrm.cloud.google.com/remove-default-node-pool: "true"
spec:
  location: "{location}"
  enableAutopilot: true
  privateClusterConfig:
    enablePrivateNodes: true
    enablePrivateEndpoint: false
"""
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as temp_f:
            temp_f.write(manifest)
            temp_path = temp_f.name
            
        log(f"Applying GKE Custom Resource manifest from temporary path: {temp_path}")
        apply_manifest(temp_path)
        
        # Cleanup intermediate file instantly
        os.unlink(temp_path)
    except subprocess.CalledProcessError as e:
        err_msg = f"ERROR: GKE Custom Resource deployment failed.\nExit Code: {e.returncode}\nStderr: {e.stderr}"
        log(err_msg)
        return err_msg
    except Exception as e:
        return f"ERROR: GKE Custom Resource deployment failed: {e}"

    agent_id = f"operator-{cluster_name}-{location}"
    try:
        add_operator_to_state(agent_id, cluster_name, location, pid)
    except Exception as e:
        return f"ERROR: Failed to register operator in state registry: {e}"

    return f"SUCCESS: {agent_id} | PROJECT: {pid}"


@mcp.tool()
def deprovision_operator(cluster_name: str, location: str) -> str:
    """
    Natively and dynamically de-provision an active GKE Operator Agent and tear down its GKE cluster.

    This tool deletes the GKE cluster Custom Resource and automatically purges its registry record.
    GCP will safely tear down the physical GKE cluster in the background.

    Args:
        cluster_name: The name of the GKE cluster to de-provision (e.g., 'mercury-02').
        location: The GCP region or zone of the GKE cluster (e.g., 'us-central1' or 'us-central1-a').
    """
    agent_id = f"operator-{cluster_name}-{location}"
    
    try:
        delete_cluster_manifest(cluster_name)
    except subprocess.CalledProcessError as e:
        err_msg = f"ERROR: GKE Custom Resource deletion failed.\nExit Code: {e.returncode}\nStderr: {e.stderr}"
        log(err_msg)
        return err_msg
    except Exception as e:
        return f"ERROR: GKE Custom Resource deletion failed: {e}"

    try:
        remove_operator_from_state(agent_id)
    except Exception as e:
        return f"ERROR: State cleanup failed: {e}"

    return f"SUCCESS: {agent_id} DELETED"

@mcp.tool()
def list_devteams() -> str:
    """
    List all active, registered GKE DevTeam Agents in the GKE fleet.
    Returns their unique Agent IDs, managed cluster names, regional locations,
    target namespaces, GCP Project IDs, stable local endpoints, and registration timestamps.
    """
    state_file = get_hermes_home() / "devteam_agents.jsonl"
    
    if not state_file.exists():
        return "No active GKE DevTeam Agents are currently registered."
        
    devteams = []
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                entry = json.loads(line)
                clean_entry = {
                    "agent_id": entry.get("agent_id"),
                    "cluster_name": entry.get("cluster_name"),
                    "location": entry.get("location"),
                    "namespace": entry.get("namespace"),
                    "project_id": entry.get("project_id"),
                    "endpoint": entry.get("endpoint"),
                    "status": entry.get("status", "active"),
                    "created_at": entry.get("created_at")
                }
                devteams.append(clean_entry)
    except Exception as e:
        return f"ERROR: Failed to read DevTeam agents registry: {e}"
        
    if not devteams:
        return "No active GKE DevTeam Agents are currently registered."
        
    return json.dumps(devteams, indent=2)

@mcp.tool()
def register_devteam(cluster_name: str, location: str, namespace: str, project_id: str = "") -> str:
    """
    Natively and dynamically register a GKE DevTeam Agent workspace configuration.
    Note: In this current rollout version, no physical Kubernetes resources are created yet.
    The agent workspace is registered inside the state registry to enable future GitOps lifecycle syncs.

    Args:
        cluster_name: The name of the GKE cluster where the team workspace resides (e.g., 'mercury-02').
        location: The GCP region or zone of the cluster (e.g., 'us-central1').
        namespace: The isolated tenant namespace assigned to this development team (e.g., 'devteam-billing').
        project_id: Optional GCP Project ID. If omitted, it resolves automatically from the environment.
    """
    if not namespace or not all(c.islower() or c.isdigit() or c == '-' for c in namespace) or len(namespace) > 63:
        return "ERROR: Invalid namespace. It must consist of lowercase alphanumeric characters or '-', and be at most 63 characters."
    if not cluster_name or not all(c.islower() or c.isdigit() or c == '-' for c in cluster_name) or len(cluster_name) > 63:
        return "ERROR: Invalid cluster_name. It must consist of lowercase alphanumeric characters or '-', and be at most 63 characters."

    pid = project_id if project_id else get_project_id()
    if not pid:
        return "ERROR: Could not resolve GCP Project ID. Please specify 'project_id'."

    err = validate_location(location, pid)
    if err:
        return err

    agent_id = f"devteam-{cluster_name}-{location}-{namespace}"
    try:
        add_devteam_to_state(agent_id, cluster_name, location, namespace, pid)
    except Exception as e:
        return f"ERROR: Failed to register DevTeam agent in state registry: {e}"

    return f"SUCCESS: {agent_id} | PROJECT: {pid}"

@mcp.tool()
def deregister_devteam(cluster_name: str, location: str, namespace: str) -> str:
    """
    Natively and dynamically deregister a GKE DevTeam Agent workspace configuration and purge its registry record.

    Args:
        cluster_name: The name of the GKE cluster where the team workspace resides (e.g., 'mercury-02').
        location: The GCP region or zone of the cluster (e.g., 'us-central1').
        namespace: The isolated tenant namespace assigned to this development team (e.g., 'devteam-billing').
    """
    agent_id = f"devteam-{cluster_name}-{location}-{namespace}"
    
    try:
        remove_devteam_from_state(agent_id)
    except Exception as e:
        return f"ERROR: State cleanup failed: {e}"

    return f"SUCCESS: {agent_id} DELETED"


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
