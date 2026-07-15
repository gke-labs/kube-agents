---
name: sre-readonly-triage
description: Standard Operating Procedure (SOP) for the Read-Only SRE Agent to perform non-mutating diagnostics, triage alerts, formulate declarative remediation patches, and submit Pull Requests via Git.
---

# SRE Read-Only Triage & PR Remediation Skill

Use this skill whenever triaging cluster alerts, responding to user troubleshooting requests, or performing periodic health checks in the **Read-Only SRE Agent Profile**.

---

## ⛔ 1. Safety Red Lines & Non-Mutation Rules

1. **NO DIRECT CLUSTER MUTATIONS:** Never attempt `kubectl apply`, `kubectl delete`, `kubectl patch`, `kubectl edit`, `kubectl scale`, or mutating `gcloud` commands against the Kubernetes cluster or cloud resources.
2. **ZERO TOKEN-WASTE ON MUTATION ATTEMPTS:** Do not waste LLM reasoning tokens or tool-calling loops trying to bypass permission errors or execute live cluster repairs. If an issue requires a fix, proceed immediately to step 3 (formulate fix manifest and open a PR).
3. **PULL REQUEST IS THE ONLY REMEDIATION OUTPUT:** All fixes must be delivered as Pull Requests against the GitOps repository for human review and merge.

---

## 🔍 2. Diagnostic Execution Loop

### Step 2.1: Context & Credentials
Fetch cluster credentials and define the incident query window ($T \pm 30\text{m}$):
```bash
gcloud container clusters get-credentials <cluster_name> --region <cluster_location>
```

### Step 2.2: Workload & Pod Status Analysis
Inspect active pod states and controller definitions:
```bash
kubectl get pods -l <selector_labels> -n <workload_namespace>
kubectl get deploy/<workload_name> -n <workload_namespace> -o yaml
```
Analyze pod status state:
- **`Pending`**: Unscheduled pod -> Query events for scheduling & resource bounds.
- **`CrashLoopBackOff`**: Repeated container crash -> Query exit code (`137` = OOMKilled; `1` = application crash).
- **`ContainerCreating` / `ImagePullBackOff`**: Volume mount or registry auth failure -> Query events & secrets.

### Step 2.3: Namespace Event Inspection
Query sorted namespace events for infrastructure failures:
```bash
kubectl get events -n <workload_namespace> --sort-by='.metadata.creationTimestamp'
```
Identify failure signatures: `FailedScheduling`, `FailedMount`, `ImagePullBackOff`, `ErrImagePull`.

### Step 2.4: Application Log Extraction
Extract verbatim exception traces and error logs:
```bash
kubectl logs <pod_name> -n <workload_namespace> --all-containers -p --tail=100
```
Isolate specific failure causes: memory limits exceeded, missing configuration keys, database connection timeouts, or read-only filesystem errors.

---

## 🛠️ 3. Declarative Remediation & PR Submission

Once the root cause is confirmed:

1. **Synthesize the Fix:** Formulate the updated YAML manifest, Helm values file, or Terraform configuration (e.g., increasing container memory request/limit, adding volume mounts, fixing image tags, or updating service selectors).
2. **Invoke `submit-suggestion`:**
   - Target GitOps repository from `/opt/data/SETTINGS.md`.
   - Create a dedicated Git branch (e.g., `fix/<workload_name>-<issue_type>`).
   - Commit the updated manifest files.
   - Open a GitHub Pull Request with a clear title and description detailing:
     - **Issue Summary**
     - **Root Cause Analysis & Verbatim Log Excerpts**
     - **Proposed Fix Details**
3. **DO NOT APPLY TO CLUSTER:** Leave the PR open for human review.

---

## 📋 4. SRE Status Report Template

Present findings to the user using this format:

```markdown
### 🚨 SRE Incident Triage Report

**Workload:** `<workload_name>` (Namespace: `<namespace>`)  
**Status:** `<Pod Status, e.g., CrashLoopBackOff (OOMKilled)>`  

#### 🔍 Root Cause Analysis
- **Observed Symptom:** `<brief description>`
- **Underlying Failure Mechanism:** `<verbatim error / log extract>`
- **Root Cause:** `<detailed technical explanation>`

#### 🛠️ Proposed Remediation (GitOps PR)
The fix has been prepared and submitted as a Pull Request for human review:
- **Pull Request:** [#<PR_NUMBER> (<PR_URL>)]
- **Changes Proposed:** `<summary of manifest changes>`

#### 📊 Telemetry & Links
- [Cloud Logging Explorer](https://console.cloud.google.com/logs/query...)
- [GKE Workload Overview](https://console.cloud.google.com/kubernetes/workload/overview...)
```
