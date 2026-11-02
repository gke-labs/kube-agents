---
name: support-escalation-bundle
metadata:
  category: Operations
description: >-
  Generates a structured, redacted Google Cloud Support escalation bundle when a GKE issue requires Google-side control plane intervention, quota adjustments, or platform bug escalation. Use when diagnostics confirm the root cause is outside customer-managed Kubernetes/GitOps state (such as stuck GKE control plane operations, regional stockouts with no fallback, or internal GKE control plane errors). Don't use when the issue can be resolved directly via customer-side manifest, node pool, or IAM configuration changes.
---

# GKE Support Escalation Bundle Skill

Use this skill when a GKE diagnostic investigation concludes that an issue cannot be remediated solely through customer-managed Kubernetes manifests, Terraform/Config Connector definitions, or project-level IAM bindings, and must be escalated to Google Cloud Customer Care.

## When to Generate an Escalation Bundle

Trigger this skill when live evidence proves any of the following conditions:

1. **Stuck or Failed GKE Control Plane Operations**: A cluster or node pool upgrade/create/repair operation (`operation-...`) is stuck in `RUNNING` or `ABORTING` beyond expected drain timeouts with no customer-side PodDisruptionBudget (`disruptionsAllowed: 0`) or webhook block.
2. **Control Plane / Master Unavailable**: The GKE API server returns persistent `5xx` errors or is unreachable while customer VPC routing, firewall rules, and authorized networks are verified healthy.
3. **Regional GCE / TPU / GPU Stockouts Without Fallback**: `ZONE_RESOURCE_POOL_EXHAUSTED` or `QUOTA_EXCEEDED` where the customer cannot shift zones/machine series and needs a quota increase request or capacity escalation.
4. **Suspected GKE Platform / Node Image Defect**: Kernel panics, `containerd` / `kubelet` regressions on unmodified COS/Ubuntu node images, or Dataplane V2 (`anetd`) datapath drops after ruling out NetworkPolicy and Conntrack exhaustion.

---

## Step 1: Collect Required Escalation Evidence (Read-Only)

Gather the exact telemetry a Google Cloud Technical Solutions Engineer (TSE) needs to begin root-cause analysis immediately without round-trip clarification questions:

```bash
# 1. Cluster identity, control plane version, release channel, and datapath provider
gcloud container clusters describe "${CLUSTER_NAME}" \
  --location="${LOCATION}" \
  --project="${PROJECT_ID}" \
  --format="yaml(id,name,location,currentMasterVersion,currentNodeVersion,releaseChannel,autopilot,networkConfig.datapathProvider,status,statusMessage)"

# 2. Recent cluster and node pool operations (capture exact operation IDs and timestamps)
gcloud container operations list \
  --location="${LOCATION}" \
  --project="${PROJECT_ID}" \
  --sort-by=~startTime \
  --limit=10 \
  --format="table(name,operationType,status,targetLink.basename(),startTime,endTime,statusMessage)"

# 3. Node pool topology, machine types, and autoscaling bounds
gcloud container node-pools list \
  --cluster="${CLUSTER_NAME}" \
  --location="${LOCATION}" \
  --project="${PROJECT_ID}" \
  --format="table(name,config.machineType,config.imageType,version,status,autoscaling.enabled,autoscaling.minNodeCount,autoscaling.maxNodeCount,locations)"

# 4. Recent Cloud Audit Log errors on container.googleapis.com
gcloud logging read \
  "resource.type=\"gke_cluster\" AND resource.labels.cluster_name=\"${CLUSTER_NAME}\" AND severity>=ERROR" \
  --project="${PROJECT_ID}" \
  --limit=20 \
  --format="table(timestamp,protoPayload.methodName,protoPayload.status.code,protoPayload.status.message)"

# 5. For NodeNotReady / PLEG / host freeze escalations: capture kubelet logs and GCE serial port output
gcloud logging read \
  "resource.type=\"k8s_node\" AND resource.labels.cluster_name=\"${CLUSTER_NAME}\" AND resource.labels.node_name=\"${NODE_NAME}\" AND log_id(\"kubelet\") AND severity>=WARNING" \
  --project="${PROJECT_ID}" \
  --limit=30 \
  --format="table(timestamp,jsonPayload.message,textPayload)"

gcloud compute instances get-serial-port-output "${NODE_NAME}" \
  --zone="${NODE_ZONE}" \
  --project="${PROJECT_ID}" \
  --port=1 | tail -n 100
```

---

## Step 2: Enforce Strict Redaction Rules

Before emitting the escalation bundle, strip or mask all sensitive material:

- **Never include** Kubernetes `Secret` data payloads, environment variable values containing passwords/API keys, bearer tokens, OAuth tokens, or private keys.
- **Keep** Google Cloud resource identifiers that Google Cloud Support requires to locate the cluster: `PROJECT_ID`, `CLUSTER_NAME`, `LOCATION`, cluster UID, `operation-...` IDs, GCE instance names, and UTC timestamps.

---

## Step 3: Emit the Escalation Bundle Template

Format the output using the following structure so the operator can paste it directly into the Google Cloud Console Support Case form:

```markdown
## Google Cloud Support Escalation Bundle

### 1. Environment & Cluster Identity

- **Project ID:** `<project_id>`
- **Cluster Name / Location:** `<cluster_name>` (`<location>`)
- **Cluster UID:** `<cluster_id>`
- **Master / Node Version:** `<currentMasterVersion>` / `<currentNodeVersion>`
- **Release Channel & Mode:** `<releaseChannel>` (`<Standard | Autopilot>`)
- **Datapath Provider:** `<LEGACY_DATAPATH | ADVANCED_DATAPATH>`
- **Incident Time Window (UTC):** `<YYYY-MM-DDTHH:MM:SSZ>` to `<YYYY-MM-DDTHH:MM:SSZ>`

### 2. Problem Statement & Business Impact

- **Symptom:** <1-2 sentence summary of the observed failure>
- **Affected Scope:** <Node pool names, zones, namespaces, and workload count impacted>
- **GKE Operation IDs:** `<operation-id-1>`, `<operation-id-2>`

### 3. Observed Evidence

- **Control Plane / Operation Status:**
  `<exact statusMessage or error code from gcloud container operations>`
- **Audit / Node Log Signatures:**
  `<timestamped log lines or event reasons>`

### 4. Customer-Side Checks Already Verified (Hypotheses Eliminated)

- [x] **PodDisruptionBudgets / Drain Blockers:** <Verified status of PDBs and webhook timeouts>
- [x] **IAM & Service Accounts:** <Verified status of node SA / Workload Identity / KMS bindings>
- [x] **VPC Networking & Quotas:** <Verified status of subnet IP ranges, firewall rules, and project quotas>
- [x] **Workload Configuration:** <Verified status of resource limits, probes, and scheduling constraints>

### 5. Specific Request for Google Cloud Support

- <Clear statement of what action is needed from Google Cloud Support, e.g., inspect internal control plane logs for operation `<operation-id>`, force-abort stuck operation, or evaluate regional capacity allocation>
```
