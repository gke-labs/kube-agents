---
name: ktlo_unhealthy_cc_triage
description: >-
  Autonomously triage and diagnose "Unhealthy Config Controller Instance" alerts.
  Use when investigating Config Controller hosting failures to determine whether the cluster has been deleted
  (to immediately auto-close false alarms) or is actively running and requires root-cause diagnosis of failed
  HealthChecks, missing bootstrap deployments, operator errors, and pod crashloops (`OOMKilled`, `VPC-SC`).
---

# Config Controller KTLO Triage & Diagnostic Skill

## Overview

You are responsible for autonomously triaging "Unhealthy Config Controller Instance" alerts strictly following the **investigation steps and decision tree outlined below in this document**. Your investigation MUST strictly use **read-only diagnostic tools** (`verify_gke_cluster`, `list_cc_healthchecks`, `get_cc_operator_status`, `list_cc_pods`, `get_cc_pod_diagnostics`, `audit_log_searcher`) to guarantee safe evaluation without disrupting customer infrastructure.

## Architectural Context & Installation Types

Before interpreting diagnostic data, recall how Config Connector / Config Controller is hosted:

- **Config Controller (Dedicated Cluster):** Fully managed GKE cluster prefixed with `krmapihost-`. Hosting components and global health monitors reside in the `krmapihosting-system` namespace.
- **GKE Add-on / Manual Install:** Run directly inside customer clusters in `cnrm-system`.

_Note: For these KTLO alerts, you will primarily operate against dedicated Config Controller management clusters._

## Triage Decision Tree

```mermaid
flowchart TD
    Start([Alert Received: Unhealthy Config Controller Instance]) --> Step1[Phase 1: Verify Cluster Existence]
    Step1 -->|Call verify_gke_cluster| Exists{Cluster Exists?}

    Exists -->|No / 404 Not Found| PathA[Deleted Cluster Path: Auto-Close False Alarm]
    PathA --> CommentClose[Post Deletion Summary & Auto-Close Ticket]

    Exists -->|Yes / Running| PathB[Active Cluster Path: Root-Cause Diagnosis]
    PathB --> Step2[Phase 2: Check Global Health & Operator]
    Step2 -->|Call list_cc_healthchecks & get_cc_operator_status| HealthResult{Health Check Signature}

    HealthResult -->|Bootstrap deployment couldn't be retrieved| CheckAudit[Phase 2b: Call audit_log_searcher]
    CheckAudit --> ReportAudit[Report Customer Deleted Bootstrap Deployment]

    HealthResult -->|Operator / Pod Failure| Step3[Phase 3: Inspect Pods & Logs]
    Step3 -->|Call list_cc_pods| FindPods[Locate Failing bootstrap-*, cnrm-*, or {component name} Pods]
    FindPods -->|Call get_cc_pod_diagnostics| ExtractLogs[Extract OOMKills, VPC-SC Errors & Tail Logs]
    ExtractLogs --> Report[Post Pre-Packaged Diagnostic Report]
```

## Investigation Workflow

### Phase 1: Cluster Lifecycle Verification (`GKE cluster already deleted`)

Start every investigation by confirming whether the target GKE cluster still exists. The alert signal relies on historical metrics data, so tickets are frequently generated even if the associated CC/GKE cluster has already been deleted.

1. **Check Cluster Existence:**
   Call `verify_gke_cluster` using the cluster name, location, and project ID provided in the ticket body.
2. **Evaluate Decision Branch:**
   - **If `exists: false` (`404 Not Found` / status `DELETED` / `ABORTING`) -> Execute Deleted Cluster Path**
   - **If `exists: true` (`status: RUNNING`) -> Execute Active Cluster Path**

### Path 1: Deleted Cluster Path (Auto-Close False Alarm)

If `verify_gke_cluster` confirms that the CC/GKE cluster no longer exists (`exists: false` / `404 Not Found`), **STOP IMMEDIATELY**.

- **Do NOT call `audit_log_searcher`, `list_cc_healthchecks`, `list_cc_pods`, or execute any custom queries/scripts.**
- **Resolve Ticket:** Post a clean, human-readable comment stating that the CC/GKE cluster no longer exists (`404 Not Found`). Extract any manual silence URL/link provided in the ticket body (`e.g., "To create a manual silence, follow the link in the ticket body"`) and include it directly in your comment so the on-call SRE can silence the alert with a single click before auto-closing the ticket.

---

### Path 2: Active Cluster Path (Root-Cause Diagnosis)

If the cluster is actively running (`exists: true`), systematically isolate the failing component using read-only diagnostics following the playbook steps.

#### Step 1: Inspect Global Health Checks & Operator Status (`Find the unhealthy checks`)

1. **Query Global Health Checks:**
   Call `list_cc_healthchecks` to retrieve all `HealthCheck` custom resources in `krmapihosting-system`.
   - Inspect `status.conditions[].message` across returned items to identify exact failing checks (`{component name}.{namespace}.{check name}`).
2. **Query Operator Status (`configconnector.krmapihosting-system.operator`):**
   Call `get_cc_operator_status` to verify whether the core `ConfigConnector` operator object (`configconnector.core.cnrm.cloud.google.com`) is reporting `status: healthy: true` during reconciliation.
   - **If `status.healthy` is not `true`:** Extract the specific error details directly from the `status` field (`status.errors` or `status.conditions[].message`) of the `ConfigConnector` object (`e.g., "If the health check fails, the specific error can be found in the status of the ConfigConnector object"`).

#### Step 2: Check Bootstrap Deployment Deletion (`Bootstrap deployment couldn't be retrieved`)

If a health check explicitly reports **"Bootstrap deployment couldn't be retrieved"** or if the `bootstrap` deployment is missing from the active cluster:

1. **Audit Deletion Actor:**
   Call `audit_log_searcher` passing `project_id`, `cluster_name`, and `location`. This checks Cloud Audit Logs (`protoPayload.methodName:delete AND "deployments/bootstrap"`) to verify whether the customer manually deleted the bootstrap deployment after instance creation.
2. **Report Findings:**
   If `audit_log_searcher` returns matching log entries, extract `protoPayload.authenticationInfo.principalEmail` and `timestamp` from the JSON payload (`e.g., "Deleted by user@domain.com at 2026-07-05T14:20:00Z"`) into the diagnostic report. If no logs are found (`[]`), report that the deletion actor could not be retrieved from recent audit logs.

#### Step 3: Diagnose Pod Crashloops & OOMKills (`Bootstrap deployment is not available`)

If `bootstrap-*`, `cnrm-*`, or **any pod matching the failing `{component name}` discovered in `status.conditions[].message` during Step 1** is unavailable, crashlooping, or failing:

1. **List System Pods:**
   Call `list_cc_pods` to list all pods across the `krmapihosting-system` namespace (`bootstrap-*`, `cnrm-*`, `git-sync`, `resource-group-controller`, etc.).
2. **Identify Degraded Pods:**
   Filter for pods exhibiting non-Running phases (`CrashLoopBackOff`, `Error`, `Pending`, `ImagePullBackOff`), high restart counts (`restarts > 0`), or matching the exact `{component name}` identified from the failing health check.
3. **Extract Detailed Diagnostics (`EVENTS and LOGS`):**
   For every degraded pod identified, call `get_cc_pod_diagnostics(pod_name)`. Examine the output for specific playbook failure signatures:
   - **OOMKill Detection (`Container Crash-looping Issue`):** Check pod status JSON and describe output for exit code `137` or `reason: "OOMKilled"`. This indicates the controller exceeded memory limits under heavy resource reconciliation load.
   - **VPC-SC Preventing Image Pulling:** Check events for `ImagePullBackOff` or `403 Permission Denied` token fetch errors caused by VPC Service Controls blocking container registry access.
   - **Stack Traces / Crash Loops (`Bootstrap pod keeps crashing`):** Inspect the pod logs returned by `get_cc_pod_diagnostics` (checking both current container logs and previous terminated crash logs when `restarts > 0`) for unhandled panics, reconciliation errors, or fatal exit errors that triggered the container restart.

#### Step 4: Prepare Human Escalation Summary

Synthesize your findings into a structured, pre-packaged root-cause summary for the on-call SRE engineer:

- **Root Cause Statement:** Clear 1-2 sentence diagnosis (`e.g., "Bootstrap pod bootstrap-pod-xyz is crashlooping due to an OOMKill (Exit Code 137) while reconciling high-volume IAM resources."` or `"Customer manually deleted deployments/bootstrap inside the active cluster after instance creation."`).
- **Global Health Condition:** Specific error string extracted from `status.conditions[].message` or `get_cc_operator_status`.
- **Evidence:** Key log lines, event traces, or audit logs retrieved during investigation.
- **Remediation Recommendation:** Link to the relevant playbook step (`e.g., advising the engineer to apply a ControllerResource memory override, fix VPC-SC rules, or advise the customer on bootstrap deletion`). _Do NOT attempt to apply mutations yourself._

## UX & Noise Reduction Guidelines

To ensure a clean, "magical" experience in Google Chat and issue trackers:

- **Silent Investigation:** Do NOT post intermediate status messages, tool execution logs, or raw JSON payloads to the user chat during your background investigation.
- **In-Place Progress Updates:** If progress reporting is required, overwrite/update a single status message in place rather than appending multiple messages.
- **Actionable Output Only:** Present only the final, formatted outcome—either the Auto-Close confirmation or the structured Diagnostic Report.
