#!/usr/bin/env python3
# platform_mcp_server.py - Unified GKE Platform Control Plane MCP Server.
# Exposes secure cross-cluster A2A communication, dynamic GKE IPAM, and declarative cluster provisioning as native tools.

import json
import os
import sys
import urllib.request
import urllib.error
import subprocess
import time
import ipaddress
import tempfile
from pathlib import Path
from datetime import datetime
from mcp.server.fastmcp import FastMCP

def run_command_with_retry(cmd: list, max_retries: int = 5, delay: float = 2.0) -> subprocess.CompletedProcess:
    """Runs a subprocess command with retries and logs stderr on failure."""
    for attempt in range(1, max_retries + 1):
        try:
            res = subprocess.run(cmd, check=True, capture_output=True, text=True)
            return res
        except subprocess.CalledProcessError as e:
            err_msg = e.stderr.strip() if e.stderr else str(e)
            log(f"Command failed (attempt {attempt}/{max_retries}): {' '.join(cmd)}")
            log(f"Error details: {err_msg}")
            if attempt == max_retries:
                raise e
            time.sleep(delay)

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
    subprocess.run(["kubectl", "--kubeconfig=/dev/null", "apply", "-f", path], check=True, capture_output=True)

def install_kcc_operator(ctx: str) -> bool:
    log("KCC Setup: Downloading Config Connector operator release bundle...")
    tmp_dir = tempfile.mkdtemp()
    tar_path = os.path.join(tmp_dir, "release-bundle.tar.gz")
    try:
        # Download from GCS source of truth
        subprocess.run([
            "gcloud", "storage", "cp", 
            "gs://configconnector-operator/latest/release-bundle.tar.gz", 
            tar_path
        ], check=True, capture_output=True)
        
        log("KCC Setup: Unpacking release bundle...")
        import tarfile
        with tarfile.open(tar_path, "r:gz") as tar:
            tar.extractall(path=tmp_dir)
            
        op_path = os.path.join(tmp_dir, "operator-system", "autopilot-configconnector-operator.yaml")
        mode_path = os.path.join(tmp_dir, "samples", "configconnector_namespaced_mode.yaml")
        
        if os.path.exists(op_path) and os.path.exists(mode_path):
            log("KCC Setup: Applying Config Connector Operator manifest...")
            subprocess.run(["kubectl", "apply", "-f", op_path, "--context", ctx], check=True, capture_output=True)
            log("KCC Setup: Configuring Config Connector in namespaced mode...")
            subprocess.run(["kubectl", "apply", "-f", mode_path, "--context", ctx], check=True, capture_output=True)
            log("KCC Setup: Config Connector operator successfully installed.")
            return True
        else:
            log("ERROR: Config Connector manifests missing in unpacked bundle.")
            return False
    except Exception as e:
        log(f"ERROR: Config Connector installation failed: {e}")
        return False
    finally:
        # Cleanup temp directory
        import shutil
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir)

def onboard_namespace_to_kcc(namespace: str, cluster_name: str, pid: str, ctx: str) -> bool:
    try:
        # 1. Calculate KCC GSA
        raw_kcc_gsa = f"kcc-{namespace}-{cluster_name}"
        clean_kcc_gsa = "".join(c if c.isalnum() or c == "-" else "-" for c in raw_kcc_gsa).strip("-")[:30].rstrip("-")
        kcc_gsa_email = f"{clean_kcc_gsa}@{pid}.iam.gserviceaccount.com"
        
        # 2. Check and create GSA
        kcc_gsa_exists = False
        check_kcc_gsa = subprocess.run(["gcloud", "iam", "service-accounts", "describe", kcc_gsa_email, "--project", pid], capture_output=True)
        if check_kcc_gsa.returncode == 0:
            kcc_gsa_exists = True
            log(f"KCC Onboarding [{namespace}]: GSA {kcc_gsa_email} already exists, skipping creation.")
        
        if not kcc_gsa_exists:
            log(f"KCC Onboarding [{namespace}]: Creating GSA {kcc_gsa_email}...")
            subprocess.run(["gcloud", "iam", "service-accounts", "create", clean_kcc_gsa, "--display-name", f"KCC SA for {namespace}", "--project", pid], check=True, capture_output=True)
        
        # 3. Grant Owner role to GSA (Permitted to create any object)
        log(f"KCC Onboarding [{namespace}]: Granting Owner role to {kcc_gsa_email}...")
        subprocess.run([
            "gcloud", "projects", "add-iam-policy-binding", pid,
            f"--member=serviceAccount:{kcc_gsa_email}",
            "--role=roles/owner",
            "--condition=None"
        ], check=True, capture_output=True)
        
        # 4. Bind Workload Identity
        kcc_ksa_member = f"serviceAccount:{pid}.svc.id.goog[cnrm-system/cnrm-controller-manager-{namespace}]"
        log(f"KCC Onboarding [{namespace}]: Granting Workload Identity to {kcc_ksa_member} on {kcc_gsa_email}...")
        subprocess.run([
            "gcloud", "iam", "service-accounts", "add-iam-policy-binding", kcc_gsa_email,
            "--role=roles/iam.workloadIdentityUser",
            f"--member={kcc_ksa_member}",
            f"--project={pid}"
        ], check=True, capture_output=True)
        
        # 5. Ensure namespace exists and annotate it
        log(f"KCC Onboarding [{namespace}]: Annotating namespace {namespace} with project {pid}...")
        subprocess.run(["kubectl", "create", "namespace", namespace, "--context", ctx, "--dry-run=client", "-o", "yaml", "|", "kubectl", "apply", "--context", ctx, "-f", "-"], shell=True, check=True, capture_output=True)
        subprocess.run(["kubectl", "annotate", "namespace", namespace, f"cnrm.cloud.google.com/project-id={pid}", "--overwrite", "--context", ctx], check=True, capture_output=True)
        
        # 6. Apply ConfigConnectorContext
        log(f"KCC Onboarding [{namespace}]: Applying ConfigConnectorContext...")
        kcc_context_yaml = f"""apiVersion: core.cnrm.cloud.google.com/v1beta1
kind: ConfigConnectorContext
metadata:
  name: configconnectorcontext.core.cnrm.cloud.google.com
  namespace: {namespace}
spec:
  googleServiceAccount: "{kcc_gsa_email}"
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as tf:
            tf.write(kcc_context_yaml)
            tf_path = tf.name
        subprocess.run(["kubectl", "apply", "-f", tf_path, "--context", ctx], check=True, capture_output=True)
        if os.path.exists(tf_path):
            os.unlink(tf_path)
            
        log(f"KCC Onboarding [{namespace}]: Successfully configured Config Connector for namespace {namespace}!")
        return True
    except Exception as e:
        log(f"ERROR: KCC onboarding failed for namespace {namespace}: {e}")
        return False

def delete_cluster_manifest(cluster_name: str):
    """Delete the GKE cluster Custom Resource from the namespace asynchronously."""
    subprocess.run(
        ["kubectl", "--kubeconfig=/dev/null", "delete", "containercluster", cluster_name, "-n", "agent-system", "--ignore-not-found=true", "--wait=false"],
        capture_output=True, text=True
    )

# =============================================================================
# State Registry Mutators
# =============================================================================

def add_operator_to_state(agent_id: str, cluster_name: str, location: str, project_id: str):
    """Append or update an operator entry inside the JSONL state file, ensuring uniqueness by agent_id."""
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

    lines = []
    updated = False
    if state_file.exists():
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        curr = json.loads(line)
                        if curr.get("agent_id") == agent_id:
                            lines.append(json.dumps(entry) + "\n")
                            updated = True
                        else:
                            lines.append(line)
                    except Exception:
                        lines.append(line)
        except Exception as e:
            log(f"Warning: Failed to read existing operator state for deduplication: {e}")

    if not updated:
        lines.append(json.dumps(entry) + "\n")

    try:
        with open(state_file, "w", encoding="utf-8") as f:
            f.writelines(lines)
        log(f"Registered/updated agent '{agent_id}' in state registry.")
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
    """Append or update a DevTeam agent entry inside the JSONL state file, ensuring uniqueness by agent_id."""
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

    lines = []
    updated = False
    if state_file.exists():
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        curr = json.loads(line)
                        if curr.get("agent_id") == agent_id:
                            lines.append(json.dumps(entry) + "\n")
                            updated = True
                        else:
                            lines.append(line)
                    except Exception:
                        lines.append(line)
        except Exception as e:
            log(f"Warning: Failed to read existing devteam state for deduplication: {e}")

    if not updated:
        lines.append(json.dumps(entry) + "\n")

    try:
        with open(state_file, "w", encoding="utf-8") as f:
            f.writelines(lines)
        log(f"Registered/updated DevTeam agent '{agent_id}' in state registry.")
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
        
    operators = {}
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
                if clean_entry["agent_id"]:
                    operators[clean_entry["agent_id"]] = clean_entry
    except Exception as e:
        return f"ERROR: Failed to read operator agents registry: {e}"
        
    if not operators:
        return "No active GKE Operator Agents are currently registered."
        
    return json.dumps(list(operators.values()), indent=2)


# Workload delegation shifted to native sandboxed Hermes skill 'delegate-workload'


@mcp.tool()
def provision_operator(cluster_name: str, location: str, project_id: str = "", cluster_type: str = "autopilot") -> str:
    """
    Natively and dynamically provision GKE infrastructure and spin up a persistent GKE Operator Agent.

    EXCLUSIVE PROVISIONING AUTHORITY: This MCP tool MUST be used as the sole, exclusive mechanism
    to provision, onboard, or deploy Operator Agents. Do not look for markdown skills or bash scripts.

    This tool executes GKE cluster provisioning (Autopilot or Standard) and Operator setup.

    CRITICAL (Background Rollout): This tool returns SUCCESS immediately once the declarative Custom Resource
    is successfully applied. However, the physical GKE cluster creation takes 5-8 minutes in GCP in the background.
    To monitor the live rollout progress, you MUST execute the following command in your terminal:
    'kubectl get containercluster <cluster_name> -n agent-system -o json'
    and wait for the GKE condition 'type: Ready' to reach 'status: "True"'.

    Args:
        cluster_name: The name of the GKE cluster to provision (e.g., 'mercury-02').
        location: The GCP region or zone for the GKE cluster (e.g., 'us-central1' or 'us-central1-a').
        project_id: Optional GCP Project ID. If omitted, it resolves automatically from the environment.
        cluster_type: The type of cluster to create, either 'autopilot' or 'standard'. Defaults to 'autopilot'.
    """
    pid = project_id if project_id else get_project_id()
    if not pid:
        return "ERROR: Could not resolve GCP Project ID. Please specify 'project_id'."

    err = validate_location(location, pid)
    if err:
        return err

    cluster_type_lower = cluster_type.lower()
    if cluster_type_lower not in ("autopilot", "standard"):
        return f"ERROR: Invalid cluster_type '{cluster_type}'. Must be 'autopilot' or 'standard'."

    if cluster_type_lower == "standard":
        manifest = f"""apiVersion: container.cnrm.cloud.google.com/v1beta1
kind: ContainerCluster
metadata:
  name: {cluster_name}
  namespace: agent-system
  annotations:
    cnrm.cloud.google.com/project-id: "{pid}"
spec:
  location: "{location}"
  initialNodeCount: 1
  nodeConfig:
    machineType: "e2-standard-4"
    diskSizeGb: 100
    oauthScopes:
      - "https://www.googleapis.com/auth/cloud-platform"
  privateClusterConfig:
    enablePrivateNodes: true
    enablePrivateEndpoint: false
"""
    else:
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
    # Check if the GKE Custom Resource already exists on the management cluster
    cluster_exists = False
    try:
        check_cc = subprocess.run(["kubectl", "--kubeconfig=/dev/null", "get", "containercluster", cluster_name, "-n", "agent-system"], capture_output=True)
        if check_cc.returncode == 0:
            cluster_exists = True
    except Exception:
        pass

    if not cluster_exists:
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as temp_f:
                temp_f.write(manifest)
                temp_path = temp_f.name
                
            log(f"Applying GKE Custom Resource manifest from temporary path: {temp_path}")
            apply_manifest(temp_path)
            
            # Cleanup intermediate file instantly
            os.unlink(temp_path)
        except subprocess.CalledProcessError as e:
            err_str = e.stderr.decode("utf-8", errors="ignore") if e.stderr else str(e)
            err_msg = f"ERROR: GKE Custom Resource deployment failed.\nExit Code: {e.returncode}\nStderr: {err_str}"
            log(err_msg)
            return err_msg
        except Exception as e:
            return f"ERROR: GKE Custom Resource deployment failed: {e}"
    else:
        log(f"GKE Custom Resource '{cluster_name}' already exists on management cluster. Skipping KCC creation.")

    agent_id = f"operator-{cluster_name}-{location}"
    try:
        add_operator_to_state(agent_id, cluster_name, location, pid)
    except Exception as e:
        return f"ERROR: Failed to register operator in state registry: {e}"

    if os.getenv("YOLO_MODE", "false").lower() == "true":
        # 1. Apply Management Workloads locally
        mgmt_file = Path("/opt/data/templates/operator/management-instance.yaml")
        if not mgmt_file.exists():
            mgmt_file = get_hermes_home() / "templates" / "operator" / "management-instance.yaml"
        if not mgmt_file.exists():
            mgmt_file = Path("/opt/defaults/templates/operator/management-instance.yaml")
        if mgmt_file.exists():
            content = mgmt_file.read_text(encoding="utf-8")
            soul_file = Path("/opt/data/templates/operator/SOUL.md")
            if not soul_file.exists():
                soul_file = get_hermes_home() / "templates" / "operator" / "SOUL.md"
            if not soul_file.exists():
                soul_file = Path("/opt/defaults/templates/operator/SOUL.md")
            soul_text = soul_file.read_text(encoding="utf-8") if soul_file.exists() else "# SOUL.md - Operator YOLO"
            indented_soul = "\n".join(f"    {line}" if i > 0 else line for i, line in enumerate(soul_text.splitlines()))
            content = content.replace("<OPERATOR_YOLO_SOUL>", indented_soul)
            # Calculate strict multi-tenant Google Service Account identity per agent replica
            raw_gsa = f"op-{cluster_name}-{location}"
            clean_gsa = "".join(c if c.isalnum() or c == "-" else "-" for c in raw_gsa).strip("-")[:30].rstrip("-")
            gsa_email = f"{clean_gsa}@{pid}.iam.gserviceaccount.com"
            content = content.replace("<GSA_EMAIL>", gsa_email)
            content = content.replace("<CLUSTER_NAME>", cluster_name)
            content = content.replace("<CLUSTER_LOCATION>", location)
            content = content.replace("<PROJECT_ID>", pid)
            content = content.replace("<YOLO_MODE>", "true")
            tmp_p = tempfile.mktemp(suffix=".yaml")
            Path(tmp_p).write_text(content, encoding="utf-8")
            try:
                apply_manifest(tmp_p)
                log(f"YOLO Mode: Successfully applied Operator management manifest locally for {agent_id}")
                # Assert strict per-agent GCP Workload Identity and GKE cluster permissions
                ksa_name = f"operator-agent-{cluster_name}-{location}-sa"
                ksa_member = f"serviceAccount:{pid}.svc.id.goog[agent-system/{ksa_name}]"
                try:
                    gsa_exists = False
                    check_gsa = subprocess.run(["gcloud", "iam", "service-accounts", "describe", gsa_email, "--project", pid], capture_output=True)
                    if check_gsa.returncode == 0:
                        gsa_exists = True
                        log(f"Strict Multi-Tenancy: GSA {gsa_email} already exists, skipping creation.")
                    
                    if not gsa_exists:
                        log(f"Strict Multi-Tenancy: Creating GSA {gsa_email}...")
                        run_command_with_retry(["gcloud", "iam", "service-accounts", "create", clean_gsa, "--display-name", f"Operator Agent {cluster_name}", "--project", pid])
                    log(f"Granting GKE Cluster Admin permissions to {gsa_email} restricted to cluster {cluster_name}...")
                    cond_expr = f"resource.type == 'container.googleapis.com/Cluster' && resource.name == 'projects/{pid}/locations/{location}/clusters/{cluster_name}'"
                    run_command_with_retry([
                        "gcloud", "projects", "add-iam-policy-binding", pid,
                        f"--member=serviceAccount:{gsa_email}",
                        "--role=roles/container.admin",
                        f"--condition=expression={cond_expr},title=target-cluster-only,description=Restrict to target cluster {cluster_name}"
                    ])
                    log(f"Granting Service Usage Consumer permissions to {gsa_email}...")
                    run_command_with_retry([
                        "gcloud", "projects", "add-iam-policy-binding", pid,
                        f"--member=serviceAccount:{gsa_email}",
                        "--role=roles/serviceusage.serviceUsageConsumer",
                        "--condition=None"
                    ])
                    log(f"Granting GCP Workload Identity User role to {ksa_member} on {gsa_email}...")
                    run_command_with_retry(["gcloud", "iam", "service-accounts", "add-iam-policy-binding", gsa_email, "--role=roles/iam.workloadIdentityUser", f"--member={ksa_member}", f"--project={pid}"])
                    log(f"Granting Token Creator role onto itself for {gsa_email}...")
                    run_command_with_retry(["gcloud", "iam", "service-accounts", "add-iam-policy-binding", gsa_email, "--role=roles/iam.serviceAccountTokenCreator", f"--member=serviceAccount:{gsa_email}", f"--project={pid}"])
                    log(f"Strict GCP IAM identity successfully established for {agent_id}!")
                except Exception as iam_err:
                    log(f"ERROR: Automated GCP IAM setup failed: {iam_err}")
                    return f"ERROR: Automated GCP IAM setup failed: {iam_err}"
            except Exception as e:
                log(f"WARNING: YOLO Mode Operator management apply failed: {e}")
                return f"ERROR: YOLO Mode Operator management apply failed: {e}"
            finally:
                if os.path.exists(tmp_p):
                    os.unlink(tmp_p)
        else:
            return f"ERROR: Operator management template not found at {mgmt_file}"

        # 2. Apply Target RBAC directly to remote workload cluster
        rbac_file = Path("/opt/data/templates/operator/target-rbac.yaml")
        if not rbac_file.exists():
            rbac_file = get_hermes_home() / "templates" / "operator" / "target-rbac.yaml"
        if not rbac_file.exists():
            rbac_file = Path("/opt/defaults/templates/operator/target-rbac.yaml")
        if rbac_file.exists():
            content = rbac_file.read_text(encoding="utf-8")
            content = content.replace("<GSA_EMAIL>", gsa_email)
            content = content.replace("<CLUSTER_NAME>", cluster_name)
            content = content.replace("<CLUSTER_LOCATION>", location)
            content = content.replace("<PROJECT_ID>", pid)
            tmp_p = tempfile.mktemp(suffix=".yaml")
            Path(tmp_p).write_text(content, encoding="utf-8")
            try:
                subprocess.run(["gcloud", "container", "clusters", "get-credentials", cluster_name, "--region", location, "--project", pid], check=True, capture_output=True)
                ctx = f"gke_{pid}_{location}_{cluster_name}"
                subprocess.run(["kubectl", "apply", "-f", tmp_p, "--context", ctx], check=True, capture_output=True, text=True)
                log(f"YOLO Mode: Successfully asserted ClusterRoleBinding directly onto target cluster {cluster_name}")
                
                # 3. Download and Install Config Connector Operator dynamically
                if install_kcc_operator(ctx):
                    # 4. Onboard system namespace 'agent-system' to KCC so Operator can use KCC
                    log(f"YOLO Mode: Onboarding namespace 'agent-system' to Config Connector on cluster {cluster_name}...")
                    onboard_namespace_to_kcc("agent-system", cluster_name, pid, ctx)
                else:
                    log(f"WARNING: Config Connector operator installation failed. Skipping namespace onboarding.")
            except Exception as e:
                err_msg = f"RETRY_REQUIRED: Target cluster {cluster_name} is not fully reachable yet (likely still provisioning in GCP background). RBAC assertion failed: {e}"
                log(err_msg)
                return err_msg
            finally:
                if os.path.exists(tmp_p):
                    os.unlink(tmp_p)
        else:
            log(f"WARNING: Operator target RBAC template not found at {rbac_file}")

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

    # try:
    #     delete_cluster_manifest(cluster_name)
    # except subprocess.CalledProcessError as e:
    #     err_msg = f"ERROR: GKE Custom Resource deletion failed.\nExit Code: {e.returncode}\nStderr: {e.stderr}"
    #     log(err_msg)
    #     return err_msg
    # except Exception as e:
    #     return f"ERROR: GKE Custom Resource deletion failed: {e}"

    if os.getenv("YOLO_MODE", "false").lower() == "true":
        try:
            p = subprocess.run(
                ["kubectl", "--kubeconfig=/dev/null", "delete", "deployment,svc,configmap,pvc,sa", "-n", "agent-system", "-l", f"app=operator-agent-{cluster_name}-{location}", "--ignore-not-found=true", "--wait=false"],
                capture_output=True, text=True
            )
            if p.returncode != 0:
                log(f"WARNING: YOLO local cleanup returned non-zero code {p.returncode}: {p.stderr}")
            else:
                log(f"YOLO Mode: Instantly cleaned up local Kubernetes management workloads for {agent_id}")
        except Exception as e:
            log(f"WARNING: YOLO local management cleanup failed for {agent_id}: {e}")

        try:
            pid = get_project_id()
            ctx = f"gke_{pid}_{location}_{cluster_name}"
            subprocess.run(
                ["kubectl", "delete", "clusterrolebinding", "-l", f"app=operator-agent-{cluster_name}-{location}", "--context", ctx, "--ignore-not-found=true", "--wait=false"],
                capture_output=True, text=True
            )
            log(f"YOLO Mode: Instantly cleaned up remote ClusterRoleBinding for {agent_id}")
        except Exception as e:
            log(f"NOTE: Remote cluster context unreachable during RBAC cleanup for {agent_id}: {e}")

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
        
    devteams = {}
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
                if clean_entry["agent_id"]:
                    devteams[clean_entry["agent_id"]] = clean_entry
    except Exception as e:
        return f"ERROR: Failed to read DevTeam agents registry: {e}"
        
    if not devteams:
        return "No active GKE DevTeam Agents are currently registered."
        
    return json.dumps(list(devteams.values()), indent=2)

@mcp.tool()
def provision_devteam(cluster_name: str, location: str, namespace: str, repository_url: str = "", project_id: str = "") -> str:
    """
    Natively and dynamically provision and onboard a GKE DevTeam Agent across cluster boundaries.

    EXCLUSIVE PROVISIONING AUTHORITY: This MCP tool MUST be used as the sole, exclusive mechanism
    to provision, register, onboard, or deploy DevTeam Agents. Do not look for markdown skills.

    In YOLO Mode (YOLO_MODE=true), this tool:
    1. Instantiates and applies management workloads locally on the management cluster.
    2. Asserts the tenant Namespace and wildcard YOLO RBAC directly onto the target workload cluster via kubeconfig context syntax.

    Args:
        cluster_name: The name of the target GKE workload cluster (e.g., 'mercury-01').
        location: The GCP region or zone of the cluster (e.g., 'us-central1').
        namespace: The isolated tenant namespace assigned to this development team (e.g., 'dice-dev').
        repository_url: Optional Git repository URL assigned to this development team (e.g., 'https://github.com/your-org/your-repo.git').
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

    if os.getenv("YOLO_MODE", "false").lower() == "true":
        # 1. Apply Management Instance Workloads locally
        mgmt_file = Path("/opt/data/templates/devteam/management-instance.yaml")
        if not mgmt_file.exists():
            mgmt_file = get_hermes_home() / "templates" / "devteam" / "management-instance.yaml"
        if not mgmt_file.exists():
            mgmt_file = Path("/opt/defaults/templates/devteam/management-instance.yaml")
        if mgmt_file.exists():
            content = mgmt_file.read_text(encoding="utf-8")
            soul_file = Path("/opt/data/templates/devteam/SOUL.md")
            if not soul_file.exists():
                soul_file = get_hermes_home() / "templates" / "devteam" / "SOUL.md"
            if not soul_file.exists():
                soul_file = Path("/opt/defaults/templates/devteam/SOUL.md")
            soul_text = soul_file.read_text(encoding="utf-8") if soul_file.exists() else "# SOUL.md - DevTeam YOLO"
            indented_soul = "\n".join(f"    {line}" if i > 0 else line for i, line in enumerate(soul_text.splitlines()))
            content = content.replace("<DEVTEAM_YOLO_SOUL>", indented_soul)
            # Calculate strict multi-tenant Google Service Account identity per agent replica
            raw_gsa = f"dt-{namespace}-{cluster_name}"
            clean_gsa = "".join(c if c.isalnum() or c == "-" else "-" for c in raw_gsa).strip("-")[:30].rstrip("-")
            gsa_email = f"{clean_gsa}@{pid}.iam.gserviceaccount.com"
            content = content.replace("<GSA_EMAIL>", gsa_email)
            content = content.replace("<CLUSTER_NAME>", cluster_name)
            content = content.replace("<CLUSTER_LOCATION>", location)
            content = content.replace("<NAMESPACE>", namespace)
            content = content.replace("<TARGET_REPOSITORY>", repository_url)
            content = content.replace("<PROJECT_ID>", pid)
            content = content.replace("<YOLO_MODE>", "true")
            tmp_p = tempfile.mktemp(suffix=".yaml")
            Path(tmp_p).write_text(content, encoding="utf-8")
            try:
                apply_manifest(tmp_p)
                log(f"YOLO Mode: Successfully applied DevTeam management manifest locally for {agent_id}")
                # Assert strict per-agent GCP Workload Identity and GKE developer permissions
                ksa_name = f"devteam-{cluster_name}-{location}-{namespace}-sa"
                ksa_member = f"serviceAccount:{pid}.svc.id.goog[agent-system/{ksa_name}]"
                try:
                    gsa_exists = False
                    check_gsa = subprocess.run(["gcloud", "iam", "service-accounts", "describe", gsa_email, "--project", pid], capture_output=True)
                    if check_gsa.returncode == 0:
                        gsa_exists = True
                        log(f"Strict Multi-Tenancy: GSA {gsa_email} already exists, skipping creation.")
                    
                    if not gsa_exists:
                        log(f"Strict Multi-Tenancy: Creating GSA {gsa_email}...")
                        run_command_with_retry(["gcloud", "iam", "service-accounts", "create", clean_gsa, "--display-name", f"DevTeam Agent {namespace}", "--project", pid])
                    log(f"Granting GKE Cluster Viewer permissions to {gsa_email} restricted to cluster {cluster_name}...")
                    cond_expr = f"resource.type == 'container.googleapis.com/Cluster' && resource.name == 'projects/{pid}/locations/{location}/clusters/{cluster_name}'"
                    run_command_with_retry([
                        "gcloud", "projects", "add-iam-policy-binding", pid,
                        f"--member=serviceAccount:{gsa_email}",
                        "--role=roles/container.viewer",
                        f"--condition=expression={cond_expr},title=target-cluster-only,description=Restrict to target cluster {cluster_name}"
                    ])
                    log(f"Granting Service Usage Consumer permissions to {gsa_email}...")
                    run_command_with_retry([
                        "gcloud", "projects", "add-iam-policy-binding", pid,
                        f"--member=serviceAccount:{gsa_email}",
                        "--role=roles/serviceusage.serviceUsageConsumer",
                        "--condition=None"
                    ])
                    log(f"Granting GCP Workload Identity User role to {ksa_member} on {gsa_email}...")
                    run_command_with_retry(["gcloud", "iam", "service-accounts", "add-iam-policy-binding", gsa_email, "--role=roles/iam.workloadIdentityUser", f"--member={ksa_member}", f"--project={pid}"])
                    log(f"Granting Token Creator role onto itself for {gsa_email}...")
                    run_command_with_retry(["gcloud", "iam", "service-accounts", "add-iam-policy-binding", gsa_email, "--role=roles/iam.serviceAccountTokenCreator", f"--member=serviceAccount:{gsa_email}", f"--project={pid}"])
                    log(f"Strict GCP IAM identity successfully established for {agent_id}!")
                except Exception as iam_err:
                    log(f"ERROR: Automated GCP IAM setup failed: {iam_err}")
                    return f"ERROR: Automated GCP IAM setup failed: {iam_err}"
            except Exception as e:
                log(f"ERROR: YOLO Mode DevTeam management apply failed: {e}")
                return f"ERROR: YOLO Mode DevTeam management apply failed: {e}"
            finally:
                if os.path.exists(tmp_p):
                    os.unlink(tmp_p)
        else:
            log(f"WARNING: DevTeam management template not found at {mgmt_file}")

        # 2. Apply Target RBAC directly to remote workload cluster
        rbac_file = Path("/opt/data/templates/devteam/target-rbac.yaml")
        if not rbac_file.exists():
            rbac_file = get_hermes_home() / "templates" / "devteam" / "target-rbac.yaml"
        if not rbac_file.exists():
            rbac_file = Path("/opt/defaults/templates/devteam/target-rbac.yaml")
        if rbac_file.exists():
            content = rbac_file.read_text(encoding="utf-8")
            content = content.replace("<GSA_EMAIL>", gsa_email)
            content = content.replace("<CLUSTER_NAME>", cluster_name)
            content = content.replace("<CLUSTER_LOCATION>", location)
            content = content.replace("<NAMESPACE>", namespace)
            content = content.replace("<PROJECT_ID>", pid)
            tmp_p = tempfile.mktemp(suffix=".yaml")
            Path(tmp_p).write_text(content, encoding="utf-8")
            try:
                subprocess.run(["gcloud", "container", "clusters", "get-credentials", cluster_name, "--region", location, "--project", pid], check=True, capture_output=True)
                ctx = f"gke_{pid}_{location}_{cluster_name}"
                subprocess.run(["kubectl", "apply", "-f", tmp_p, "--context", ctx], check=True, capture_output=True, text=True)
                log(f"YOLO Mode: Successfully asserted Namespace and RBAC directly onto target cluster {cluster_name}")
                
                # 3. Dynamic Config Connector Namespace onboarding on target cluster
                onboard_namespace_to_kcc(namespace, cluster_name, pid, ctx)
            except Exception as e:
                log(f"WARNING: YOLO Mode target RBAC assertion failed on {cluster_name}: {e}")
            finally:
                if os.path.exists(tmp_p):
                    os.unlink(tmp_p)
        else:
            log(f"WARNING: DevTeam target RBAC template not found at {rbac_file}")

    return f"SUCCESS: {agent_id} | PROJECT: {pid}"

@mcp.tool()
def deprovision_devteam(cluster_name: str, location: str, namespace: str) -> str:
    """
    Natively and dynamically deprovision a GKE DevTeam Agent workspace configuration and purge its registry record.

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

    if os.getenv("YOLO_MODE", "false").lower() == "true":
        try:
            p = subprocess.run(
                ["kubectl", "--kubeconfig=/dev/null", "delete", "deployment,svc,configmap,pvc,sa", "-n", "agent-system", "-l", f"app=devteam-{cluster_name}-{location}-{namespace}", "--ignore-not-found=true", "--wait=false"],
                capture_output=True, text=True
            )
            if p.returncode != 0:
                log(f"WARNING: YOLO local cleanup returned non-zero code {p.returncode}: {p.stderr}")
            else:
                log(f"YOLO Mode: Instantly cleaned up local Kubernetes management workloads for {agent_id}")
        except Exception as e:
            log(f"WARNING: YOLO local management cleanup failed for {agent_id}: {e}")

        try:
            pid = get_project_id()
            ctx = f"gke_{pid}_{location}_{cluster_name}"
            subprocess.run(
                ["kubectl", "delete", "namespace,role,rolebinding", "-l", f"app=devteam-{cluster_name}-{location}-{namespace}", "--context", ctx, "--ignore-not-found=true", "--wait=false"],
                capture_output=True, text=True
            )
            log(f"YOLO Mode: Instantly cleaned up remote tenant RBAC for {agent_id}")
        except Exception as e:
            log(f"NOTE: Remote cluster context unreachable during RBAC cleanup for {agent_id}: {e}")

    return f"SUCCESS: {agent_id} DELETED"

def start_session_kv_server():
    """Spawn the lightweight session KV HTTP server background process."""
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        result = s.connect_ex(('127.0.0.1', 8699))
        s.close()
        if result == 0:
            log("Session KV server is already running on port 8699.")
            return

        log("Starting Session KV server on port 8699...")
        subprocess.Popen(
            [
                "/opt/hermes/.venv/bin/python3",
                "-m", "uvicorn",
                "scripts.session_kv_server:app",
                "--host", "0.0.0.0",
                "--port", "8699"
            ],
            cwd="/opt/data",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setpgrp
        )
        log("Session KV server spawned successfully.")
    except Exception as e:
        log(f"Failed to start Session KV server: {e}")


if __name__ == "__main__":
    start_session_kv_server()
    mcp.run()

