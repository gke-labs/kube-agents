---
name: gke-stockout-investigator
description: Act upon GKE cluster-autoscaler scale-up failure and capacity constraint alerts, diagnose them using ComputeClass debugging guidelines, and submit a remediation Pull Request (or post a read-only chat diagnostic report when no GitOps repository is configured).
---

# GKE Capacity Constraint Diagnosis & Remediation Skill

This skill guides the Platform Agent on how to handle, diagnose, and remediate (or report, in chat-only mode) GKE capacity constraint and scale-up failure alerts (`scale.up.error.out.of.resources` or equivalent) received via Google Cloud Pub/Sub.

## Workflow

### 1. Notify User of Background Investigation Start

When investigating a **background Pub/Sub alert** (no interactive chat requester subscribed), call `send_notification` (or `mcp_platform_control_send_notification`) once before starting diagnostics as one short lowercase line ending with a period (include `(<cluster_name>)` before the period only if `<cluster_name>` was supplied in the alert):

```json
send_notification(message="checking <workload_name> in <namespace>.")
```

Skip `send_notification` on interactive chat asks (the requester's thread receives live progress and `kanban_complete.result` automatically). For background Pub/Sub alerts, invoke `send_notification` as a real tool call when an outbound channel is configured (never plain markdown text), then proceed directly to Step 2.

> [!IMPORTANT]
> When running in a background or PubSub context, NEVER use the `execute_code` tool or write Python scripts/subshells, as they trigger command approval safeguards and block indefinitely waiting for human approval. Only execute standard command-line tools directly (`kubectl`, `gh`, `gcloud`) or dedicated tools like `send_notification`.

### 2. Pre-Diagnosis Verifications (Duplicate PRs & False Signal Checks)

After sending the initial notification, perform two critical safety checks to see if you should stop immediately:

#### A. Check Operating Mode & Existing Pull Requests (Duplicate Prevention)

Determine the operating mode (`MODE=gitops` for background alerts or explicit PR asks with a registered repository, or strictly read-only `MODE=chat-only` otherwise), and in GitOps mode inspect open Pull Requests:

1. **Mandatory first check (every run, including interactive diagnostic asks):** Always run `gitops_preflight.py` before inspecting clusters so you know whether a GitOps repository is registered (needed for both mode selection and the required no-PR line in Step 6-Chat; reads `/etc/gitops/managed_repos` and `context_repos` — do **not** run shell file tests or `kubectl get configmap` for gitops state, because the ConfigMap env var is unset and the ambient context may not be the host cluster):

   ```bash
   python3 "$HERMES_HOME"/scripts/gitops_preflight.py --list
   ```

   - **Exception to `SOUL.md` `NO_REGISTERED_REPO` stop (`MANAGED: none` only)**: On background alerts or diagnostic/read-only asks (where the requester did not demand opening a PR), if `gitops_preflight.py --list` exits `3` with exact `MANAGED: none` (no repository in `/etc/gitops/managed_repos`), do **NOT** call `kanban_block`. Enter **Chat-Only Diagnostic Mode (`MODE=chat-only`)**: skip `vcs.py` and `submit_suggestion.py`, proceed directly to **Step 2 §B**, stay strictly read-only (never modify, patch, apply, or delete cluster resources), and — **even when the card body says `Propose only` or asks for diagnosis only** — ALWAYS include `"No GitOps repository is configured, so no pull request was opened."` in `kanban_complete.result` (never say the request asked for diagnosis only and never offer to open a PR when `MANAGED: none`).
   - **If `MANAGED: none` (exit `3`) when an interactive requester explicitly asked to open a PR, or if `MANAGED: none usable` (exit `3`) / any preflight error (`STATE_UNREADABLE`, exit `5`, timeout) occurs on a background alert or explicit PR ask**: do **NOT** enter `MODE=chat-only` and do **NOT** report that no repository is configured when `MANAGED: none usable` is returned; on a background alert where Step 1 notified and outbound chat is enabled, send a concise plain-language `send_notification` first, then follow `SOUL.md`'s stop rule and call `kanban_block(kind="needs_input")` passing the preflight's fix instruction in plain language (strip raw `NO_REGISTERED_REPO:` / `STATE_UNREADABLE:` tokens and the internal `Stop now: ... kanban_block(...)` directive). On interactive diagnostic-only asks (no PR requested), stay in read-only `MODE=chat-only` and note in `## What to do` that the configured repository entry is unusable or unreadable (never claiming no repository is configured on `MANAGED: none usable`).
   - **If `MANAGED:` lists one or more repositories (exit `0`)**: apply `SOUL.md` §3 item 3 (_"Propose in the reply unless asked to submit"_):
     - **Interactive diagnostic/investigate asks** (no request to open a PR): enter strictly read-only **`MODE=chat-only`** — skip `vcs.py` (Step 2 §A.2–A.3), `submit_suggestion.py`, and Steps 3A/6/7; proceed directly to **Step 2 §B** through **Step 6-Chat**. Because `gitops_preflight.py --list` exited `0` with a concrete `<repo>`, NEVER claim no repository is configured — include `"No pull request was opened because this request asked for diagnosis only; <repo> is registered if you want a remediation PR."` verbatim in `kanban_complete.result` and end with a `[next: ...]` button line per Step 6-Chat.
     - **Background Pub/Sub alerts or interactive asks explicitly requesting a PR**: run **`MODE=gitops`**. Pass the exact `MANAGED:` `<owner>/<repo>` or `<host>/<path>` value as `--repo` on every `vcs.py` and `submit_suggestion.py` call (omitting `--repo` fails resolution and skips the duplicate check).

2. _(GitOps Mode only)_ Extract the EXACT workload name from the alert payload (e.g., `frontend-web-app`, `ml-training-job-gpu`, `data-warehouse-analytics`, `llm-inference-service`), and ask the forge for the open PRs on that workload's branch alone:

   ```bash
   V="$HERMES_HOME"/skills/version-control/scripts/vcs.py
   python3 "$V" proposal list --repo "<repo>" --state open \
     --source "platform-agent/remediate-stockout-<exact_workload_name>"
   ```

   `--source` is what makes this a duplicate check rather than a guess. Listing every open PR and matching names here reads only the newest page, so on a busy repository the one you are looking for is not in the answer and the check passes when it should have stopped you. **CRITICAL**: do not widen the query and match branch names yourself. A PR for a DIFFERENT workload is on a different branch and will not come back — it is not a duplicate, and it is not a reason to stop.

3. _(GitOps Mode only)_ **If that list is non-empty — a PR is already open for THIS SPECIFIC workload**:
   - Immediately STOP further diagnostic, workspace, branch, and PR work.
   - Do NOT update, edit, modify, or rewrite the existing open PR description or contents.
   - Do NOT run any further diagnostics, do NOT search the workspace, do NOT create a new branch, and do NOT submit another PR.
   - Finish via `kanban_complete` with a short `summary` and a `result` opening `"**<workload_name> in <namespace> (<cluster_name>) already has an open remediation PR.**\n\nExisting pull request: [PR #<number>](<url>)."` (omit `(<cluster_name>)` if unresolved; keep Line 1 plain bold text without an inline link); for a background alert where Step 1 notified and outbound chat is enabled, send that same stand-down via `send_notification` first (skip on interactive asks).

#### B. Determine if the Alert is a Real Issue or a False Signal

A scale-up alert is a "false signal" if the cluster has already recovered (e.g. the unschedulable pods have been scheduled, deleted, or the issue was transient).

1. **Resolve the target `<cluster_context>` first**: Identify `<workload_name>`, `<namespace>`, and any `<cluster_name>`, `<location>`, or `<project_id>` from the alert payload or ask (e.g. `jsonPayload.noDecisionStatus.noScaleUp.unhandledPodGroups` or `resource.labels.cluster_name`). Never check only the default management context when the fleet has multiple clusters:
   - Call `fleet_scope()` first (plus `list_cluster_profiles()` if available) to determine the allowed `projects` and any `excluded_clusters`; never probe clusters listed in `excluded_clusters`.
   - Inspect already-configured kubeconfig contexts in its own single command:
     ```bash
     kubectl config get-contexts -o name
     ```
   - If `<cluster_name>` is known (from the alert or user ask) and is not in `excluded_clusters`, ensure its context (`gke_<project_id>_<location>_<cluster_name>`) is loaded if missing from `get-contexts` (run `gcloud container clusters get-credentials "<cluster_name>" --location="<location>" --project="<project_id>"` if needed).
   - If `<cluster_name>` is omitted or unknown, check `fleet_scope()`; if `declared` is `false` (`projects` is `[]`), first enumerate accessible projects via `gcloud projects list --format="value(projectId)"` (or fall back to the active project). If `fleet_scope()` reports non-empty `unread` projects or an `ERROR:` refusing project enumeration, note that unverified scope rather than claiming an unfound workload is healthy. List non-excluded clusters per project and load credentials before probing:
     ```bash
     gcloud container clusters list --project="<project_id>" --format="table(name,location,status)"
     ```
   - Probe each candidate context one command at a time (never combine `kubectl` context enumeration and `kubectl --context=...` in the same shell command, and never pipe `kubectl` into `grep -q`):
     ```bash
     CLUSTER_CONTEXT="<resolved_context>"
     kubectl --context="$CLUSTER_CONTEXT" get deployment,statefulset,daemonset,job,replicaset,pods -n <namespace> --ignore-not-found
     ```
2. Once `<workload_name>` is located (or `<cluster_name>` is resolved), set `CLUSTER_CONTEXT="<resolved_context>"` in the same terminal call whenever passing `--context="$CLUSTER_CONTEXT"` (each `terminal` call starts a fresh shell), and check the workload pods:
   ```bash
   CLUSTER_CONTEXT="<resolved_context>"
   kubectl --context="$CLUSTER_CONTEXT" get pods -n <namespace> -o wide
   ```
3. Check if there are any pods for this workload currently in `Pending` state with scheduling errors.
4. If there are pending pods, describe them on `<cluster_context>` to verify the event log:
   ```bash
   CLUSTER_CONTEXT="<resolved_context>"
   kubectl --context="$CLUSTER_CONTEXT" describe pod <pod_name> -n <namespace>
   ```
   Check if the events contain `FailedScheduling` with messages indicating lack of resources (e.g., "0/N nodes are available", "out of resources", "quota exceeded", "didn't match Pod's node affinity/selector") or `NotTriggerScaleUp` events. When pods use a custom `ComputeClass` or request GPUs, affinity/selector mismatches indicate that GKE cannot provision nodes matching the compute class due to zonal quota or capacity limits, and this MUST be treated as an active quota or capacity constraint issue (not a false signal).
5. **Unverified scope check (when the workload was not found):** Block with `kanban_block(kind="capability")` ONLY when the target scope could not be verified: a named `<cluster_name>`/`<project_id>` could not be read (project in `fleet_scope().unread`, `fleet_scope()` `ERROR:`, or unreachable), or no cluster was named and `fleet_scope().unread` is non-empty, `fleet_scope()` returned `ERROR:`, or any candidate project/cluster could not be listed or read. Never block over unrelated projects in `fleet_scope().unread` once a named cluster was read cleanly and `<workload_name>` was absent there. On a background alert where Step 1 notified and outbound chat is enabled, send a concise plain-language `send_notification` before calling `kanban_block`.
6. **False-signal / non-capacity stop (both `MODE=gitops` and `MODE=chat-only`):** If there are **no pods for this workload currently in `Pending` state with scheduling errors** on `<cluster_context>` (all `Running`/`Ready` or already scheduled, e.g., `ContainerCreating`, `CrashLoopBackOff`), OR an alerted workload is absent on its verified target cluster (or across all non-excluded clusters when unnamed, `unread` is empty, no `ERROR:`, and every candidate cluster was read cleanly):
   - Immediately STOP remediation. Do **NOT** call `kanban_block`, do not search a GitOps workspace, and do not propose changes.
   - For a background alert where Step 1 notified and outbound chat is enabled, call `send_notification`, then finish via `kanban_complete(summary=..., result=...)` (omit `(<cluster_name>)` if unresolved; never write `"false signal"` in `result`): if pods exist with no `Pending` scheduling errors, `"**<workload_name> in <namespace> (<cluster_name>) has no Pending pods with scheduling errors; no capacity remediation is needed.**"` with the observed pod state; if absent across verified clusters, `"**<workload_name> in <namespace> was not found across verified clusters; no capacity remediation is needed.**"` (skip `send_notification` on interactive asks).

### 3. Parse Alert Details & Gather Manifests (`MODE=gitops` or `MODE=chat-only`)

If Step 2 checks pass (real active capacity constraint or scale-up failure, and no duplicate PR in GitOps mode):

1. **Parse details**: Extract the GKE cluster name and location (region/zone) from the alert details.
2. **Gather manifests based on your operating mode** (choose **Option A** in `MODE=gitops` or **Option B** in `MODE=chat-only`).

#### Option A: GitOps Mode (`MODE=gitops` — background alert or PR-requesting ask with a registered repo)

Take a working copy using `submit_suggestion.py prepare`. The pod is not a git checkout; `prepare` brings the GitOps repository down onto this filesystem, stands you on the remediation branch, and prints one JSON line:

```bash
python3 "$HERMES_HOME"/skills/submit-suggestion/scripts/submit_suggestion.py prepare \
  --repo "<repo>" \
  --branch "platform-agent/remediate-stockout-<workload_name>"
```

```json
{
  "workspace": "/opt/data/scratch/vcs/github__acme__fleet__platform-agent__remediate-stockout-frontend-web-app",
  "repo": "acme/fleet",
  "branch": "platform-agent/remediate-stockout-frontend-web-app",
  "base": "main",
  "started_from": "main",
  "proposal": ""
}
```

**Keep that whole line — every step from here works inside `workspace`.**

**Branch reuse and refusals:** `prepare` deletes any spent branch from a closed/squash-merged PR for this workload automatically. If `prepare` refuses the branch name:

- `NOT_SPENT` or `BRANCH_NOT_OURS` (unmerged revisions, non-install PR, full PR page, or open PR): treat as someone else's work — report the code and branch and stop.
- `BRANCH_MOVED`, `OPEN_PROPOSAL`, `FORGE_CALL_FAILED`, `GIT_FAILED`, or no code: run `prepare` once more before reporting.
- `FORGE_RATE_LIMITED` or `FORGE_UNAVAILABLE`: wait a few minutes and run `prepare` once more.
- Any other refusal: report it and stop. Never append a suffix to bypass a refusal — Step 2 §A queries `platform-agent/remediate-stockout-<workload>` only, so any other branch breaks duplicate detection.

> [!CAUTION]
> **Run plain `git` inside the printed `workspace` only (`add`, `commit`, `diff`);** never call `/opt/vcs/libexec/git` or invented paths like `/opt/data/workspace`, and never run `git checkout main`, `git pull`, or `git checkout -b` (`prepare` already checked out the branch from `base`, and `git` has no network credentials).

- **Search the workspace**: Locate YAML manifests **inside `workspace`** with targeted searches (never `.*`): `<workspace>/agents/platform/skills/gke-compute-classes/assets/` or `search_files(pattern="compute-class")`; `<workspace>/deployment/` or `search_files(pattern="deployment")`.

#### Option B: Chat-Only Diagnostic Mode (`MODE=chat-only` — no GitOps repo or diagnostic-only ask)

Do **NOT** call `submit_suggestion.py` (`prepare` or `submit`), do **NOT** call `vcs.py`, and do **NOT** modify, patch, or delete cluster resources (`zero-write`). Instead, inspect the live workload (resolving a `ReplicaSet` pod owner back to its owning `Deployment` when an alert carries a `ReplicaSet` name), scheduling events, storage classes, `ComputeClass` manifests, and (when node-pool autoscaler bounds block scaling) GKE node-pool configuration directly on `<cluster_context>` with read-only commands:

```bash
CLUSTER_CONTEXT="<resolved_context>"
# If the pending pod owner is a ReplicaSet, inspect its parent controller:
kubectl --context="$CLUSTER_CONTEXT" get replicaset <replicaset_name> -n <namespace> --ignore-not-found -o jsonpath='{.metadata.ownerReferences[0].kind}/{.metadata.ownerReferences[0].name}'
kubectl --context="$CLUSTER_CONTEXT" get deployment,statefulset,daemonset,job,replicaset -n <namespace> -o yaml
kubectl --context="$CLUSTER_CONTEXT" get computeclasses -o yaml
kubectl --context="$CLUSTER_CONTEXT" get storageclass -o yaml
kubectl --context="$CLUSTER_CONTEXT" get events -n <namespace> --sort-by='.lastTimestamp'
gcloud container node-pools list --cluster="<cluster_name>" --location="<location>" --project="<project_id>" --format=yaml
```

_(If `kubectl --context="$CLUSTER_CONTEXT" get computeclasses` or `kubectl --context="$CLUSTER_CONTEXT" get storageclass` returns `NotFound` or `Forbidden`, continue diagnosing from the pod, ReplicaSet/controller, event, and node-pool outputs.)_

### 4. Diagnose Capacity, Quotas, and Resource Usage

**Efficiency Directive**: Execute diagnostic commands efficiently (in both `MODE=gitops` and `MODE=chat-only`). Combine checks into a single step where possible. Do not spend excessive turns on repetitive queries. Once diagnostics are gathered, proceed to Step 5 and then either Step 6–7 (`MODE=gitops`) or Step 6-Chat (`MODE=chat-only`).

Before proposing any configuration changes (e.g., adding fallbacks, shrinking VM shapes, or reserving resources), execute the following diagnostic commands to check GCP quotas, reservations, actual workload resource utilization, and Spot VM availability advice.

#### A. Quota Verification

Verify that the proposed machine families, CPU, or GPU metric counts are within the region's quota limits:

```bash
gcloud compute regions describe "<region>" --project="<project_id>" --format="json(quotas.filter(metric=CPUS))"
gcloud compute regions describe "<region>" --project="<project_id>" --format="json(quotas.filter(metric=NVIDIA_L4_GPUS))"
```

_Note: Filter by other metric names (e.g., `N4_CPUS`, `C4_CPUS`, `NVIDIA_T4_GPUS`, `NVIDIA_A100_GPUS`) to inspect specific hardware._

#### B. Reservations Check

Check if any zonal reservations are available for the target workload's machine type to guarantee compute capacity:

```bash
gcloud compute reservations list --project="<project_id>" --format="json"
```

#### C. Actual Workload Resource Usage

Before proposing resource reservations or changing VM shapes, analyze actual usage and account for potential spikes. Use:

```bash
CLUSTER_CONTEXT="<resolved_context>"
# Get node CPU/memory utilization summary
kubectl --context="$CLUSTER_CONTEXT" top node

# Fetch raw metrics from the metrics API server
kubectl --context="$CLUSTER_CONTEXT" get --raw "/apis/metrics.k8s.io/v1beta1/nodes"

# Get pod CPU/memory utilization summary
kubectl --context="$CLUSTER_CONTEXT" top pod -n <namespace>
```

#### D. Spot VM Availability and Pricing Advice

If configuring fallback Spot instances or diagnosing GPU capacity constraints, use the Spot advice APIs to check obtainability and preemption risk across target zones:

1. **VM & GPU Availability Advice**:
   ```bash
   gcloud beta compute advice capacity \
       --provisioning-model=SPOT \
       --instance-selection-machine-types="g2-standard-4,g2-standard-12,n1-standard-4" \
       --target-distribution-shape=ANY \
       --size=1 \
       --region="<region>" \
       --project="<project_id>" \
       --format="json"
   ```
2. **Preemption Rate and Price History**:
   ```bash
   gcloud beta compute advice capacity-history \
       --provisioning-model=SPOT \
       --machine-type=g2-standard-4 \
       --types=PREEMPTION,PRICE \
       --region="<region>" \
       --project="<project_id>" \
       --format="json"
   ```

#### E. Autoscaler Status & Backoff Loop Verification

To check if the cluster autoscaler is actively scaling, stalled, or stuck in a priority backoff reset loop:

```bash
CLUSTER_CONTEXT="<resolved_context>"
kubectl --context="$CLUSTER_CONTEXT" get configmap cluster-autoscaler-status -n kube-system -o yaml
```

_Note: The `.data.status` field in this ConfigMap is a multi-line text block. Parse it as text, locate the `NodeGroups` section, and check if any groups show `BackoffInfo` with `BackoffUntil` timestamps._

**RBAC Fallback:** If this command returns `Forbidden` or is otherwise unavailable, do not fail. Fall back to analyzing pod events for `NotTriggerScaleUp` or `FailedScheduling` events, and rely on `gcloud compute regions describe` and Spot capacity advice.

#### F. CapacityBuffer Readiness Check

`CapacityBuffer` (`autoscaling.x-k8s.io/v1beta1`, GKE 1.35.2+, shortName `cb`; not `autoscaling.gke.io`) placeholder pods live inside Cluster Autoscaler memory and do not appear in `kubectl get pods`. During triage, check whether an existing standby buffer targeting the same `ComputeClass` was already failing before pods went `Pending` (pass `--context="$CLUSTER_CONTEXT"` when targeting another cluster):

```bash
kubectl get capacitybuffers.autoscaling.x-k8s.io -A
```

If any buffer targets the affected `ComputeClass` or namespace, inspect its conditions and events (`kubectl describe capacitybuffer.autoscaling.x-k8s.io <name> -n <namespace>`):

- **`ReadyForProvisioning=False`**: Configuration, `PodTemplate`, or billing-model error blocked placeholder pods. Quote `condition.message` verbatim.
- **`LimitedByQuotas=True`**: Namespace `ResourceQuota` capped or zeroed `status.replicas` (`0` = fully blocked). Quote `condition.message` verbatim.
- **`Provisioning=False`**: Controller failed to inject placeholder pods (e.g., `FailedToGetPodTemplate`, `FailedToMakeFakePods`), excluding benign `reason: BufferIsEmpty` when `spec.replicas == 0` or a dynamic `scalableRef` buffer resolves to `status.replicas == 0` without `LimitedByQuotas=True`. Quote `condition.message` verbatim.
- **Steady-state `Provisioning=True` (`reason: FakePodsInjected`) & scale-up starvation**: Upstream `v1beta1` emits `ReadyForProvisioning`, `LimitedByQuotas`, and `Provisioning` only (no `Ready` condition). Inspect `NotTriggerScaleUp` / `TriggeredScaleUp` events on the `CapacityBuffer` object (plus `Ready=False` or `status.readyReplicas < status.replicas` past 15 min if emitted) to detect scale-up starvation or quota exhaustion on the backing `ComputeClass`; remediate fallback priorities on the `ComputeClass` without mutating `CapacityBuffer` replicas or strategy.

**Version / RBAC Fallback:** On clusters earlier than GKE 1.35.2 (`the server doesn't have a resource type`) or `Forbidden`, note `CapacityBuffer` status was absent/unreadable and continue triage.

_CRITICAL MANDATE_: Run quota (`gcloud compute regions describe`), Spot capacity advice (`gcloud beta compute advice capacity`), capacity history (`gcloud beta compute advice capacity-history`), and attempt autoscaler status (`kubectl get configmap cluster-autoscaler-status`) and CapacityBuffer readiness (`kubectl get capacitybuffers.autoscaling.x-k8s.io -A`). In `MODE=gitops`, record all executed commands (including denied attempts) under `- **Checks Performed**:` in the PR. In `MODE=chat-only`, omit raw `bash` blocks or CLI flags from `send_notification` and `kanban_complete` `result`; summarize the facts plainly instead.

### 5. Diagnose Using ComputeClass Debugging Guidelines

Inspect the target `ComputeClass` and workload manifests (in the printed `workspace` for `MODE=gitops`, or in the live `kubectl get ... -o yaml` output from Step 3 for `MODE=chat-only`), checking against the following debugging rules:

#### Rule A: Lack of Zone/Family Fallbacks

- **Problem**: The ComputeClass `priorities[]` is pinned to a single machine family or a single zone, leaving no alternative when GCE encounters a zonal capacity constraint.
- **Fix**: Propose adding fallback priorities (additional machine families like `n4`, `c4`, `n2` or other zones within the region).

#### Rule B: Large VM Shape Scarcity (>32 vCPUs)

- **Problem**: The workload requests very large VMs (>32 vCPU) which draw from thinner capacity pools and are highly prone to capacity constraints.
- **Fix**:
  - If the workload is horizontally-scalable (e.g., stateless app with multiple replicas, batch job), propose updating the workload manifest to use smaller replicas (e.g., ≤32 vCPUs) and adding smaller-core fallback priorities to the ComputeClass.
  - If the workload is NOT horizontally-scalable (e.g., a single large monolithic database or inference server), do NOT shrink the shape. Instead, vary the machine family (e.g., fallback from C3 to N2/N4) and zones.

#### Rule C: Stateful Disk Generation Mix

- **Problem**: For stateful workloads using Persistent Volumes (PVs), Gen 2 VMs (e.g., `n2`, `n2d`) and Gen 4 VMs (e.g., `c4`, `n4` with Hyperdisk) are mixed in the same `priorities[]` array, causing PV attachment deadlocks.
- **Fix**: Remove the mixed generations. The priority list for a PV-attached workload must stick to all Gen 2 or all Gen 4 machine families.

#### Rule D: Missing On-Demand Floor

- **Problem**: The priority list contains only Spot instances without an On-Demand floor. If Spot is exhausted, the workload stays `Pending`.
- **Fix**: Add a lower-priority On-Demand priority rule at the end of the `priorities[]` array to act as a safety floor.

#### Rule E: Regional Scarcity (Specialized Hardware, e.g., GPUs/TPUs)

- **Problem**: The requested specialized hardware (e.g., Nvidia H100, L4, or TPU v5e) is unavailable across all zones in the target region.
- **Fix**: Recommend migrating the workload and its infrastructure to another GCP region where capacity is available, or changing the application architecture to use a more available hardware class.

#### Rule F: Regional Quota Exceeded Violation (`quota exceeded` / GPU Limit Cap)

- **Problem**: A workload requests more total resources (CPUs or GPUs) than the regional quota limit configured for the project in that region (e.g., requesting 32 L4 GPUs when `gcloud compute regions describe us-central1` shows the `NVIDIA_L4_GPUS` quota limit is 24).
- **Fix**: Identify this explicitly as a **Regional Quota Exceeded Violation** in the diagnosis. Propose adjusting the workload deployment manifest to cap total requested GPUs/CPUs to fit strictly within the regional quota limit (e.g. reducing replicas from 4 to 3 so total GPUs = 24), and create a `ComputeClass` providing multi-zone fallback capabilities.

#### Rule G: CCC Priority Starvation & Reset Loop (Excessive Granular Machine Types) [ccc-priority-starvation]

> [!IMPORTANT]
> **MANDATORY PRIORITY CHECK**: If `spec.priorities` has **>10 granular `machineType` rules** or >200 rule-to-zone permutations (Flex Advisor cache limit), do NOT add more `machineType` rules — auto-compress by replacing all granular rules with 4 `machineFamily` rules (`n4`, `c3`, `n2`, `e2`).

- **Problem & Diagnosis**: Excessive granular `machineType` rules trigger a Cluster Autoscaler shared-backoff reset loop that starves lower-priority fallbacks (`n2`, `e2`), leaving pods `Pending`. Check `spec.priorities` (>10 `machineType` rules or >200 permutations across `location.zones`) and `cluster-autoscaler-status` (or pod events if RBAC-denied) for continuous `backoffInfo` blocks while the primary group retries for >5 min.
- **Fix**: Replace the granular `machineType` list with 4 family-level priorities (`machineFamily`: `n4`, `c3`, `n2`, `e2`).

#### Rule H: Hyperdisk Incompatibility with Older Generation Machines [ccc-hyperdisk-incompatible]

- **Problem & Diagnosis**: A Hyperdisk workload (`hyperdisk-balanced`, `hyperdisk-throughput`, `hyperdisk-extreme`, or Hyperdisk CSI `StorageClass`) uses a CCC with Gen 3/4 primary machines (`c3`/`c4`) and incompatible Gen 2 fallbacks (`c2`, `n2`, `e2`), causing scale-up failure on fallback.
- **Fix**: Replace incompatible Gen 2 fallbacks (`c2`, `n2`, `e2`) with Hyperdisk-compatible families (`c3`, `c4`, `n4`, `c3d`).

### 6-Chat. Chat-Only Diagnostic Report (`MODE=chat-only` — No PR, Zero Writes)

If `MODE=chat-only` (exact `MANAGED: none` with no GitOps repository configured, or an interactive diagnostic ask where the requester did not ask to open a pull request):

- **STOP before file edits, commits, or PR creation**: skip **Steps 6 and 7** completely. Do **NOT** invoke `submit_suggestion.py`, `vcs.py`, `git`, or any mutating `kubectl` / `gcloud` commands.
- **State plainly why no PR was opened (mandatory in `kanban_complete.result` and `send_notification`, outside code blocks — determined strictly by your `gitops_preflight.py --list` exit code, NOT by `Propose only` in the card body)**:
  1. **Exact `MANAGED: none` (exit `3`) — takes precedence even on interactive diagnostic asks and even when the card body ends with `Propose only` (NEVER say "diagnosis only" and NEVER offer to open a PR when no repository is configured)**: open `## What to do` verbatim with `"No GitOps repository is configured, so no pull request was opened."`
  2. **`MANAGED: none usable` / unreadable (exit `3` or `5` on an interactive diagnostic ask — NEVER claim no repository is configured and NEVER offer to open a PR)**: open `## What to do` noting the configured repository entry is unusable or unreadable and no pull request was opened.
  3. **ONLY when `gitops_preflight.py --list` exited `0` and printed a concrete `<repo>` under `MANAGED:` (NEVER when `MANAGED: none`, and NEVER claim no repository is configured when `<repo>` was printed)** on an interactive diagnostic ask: open `## What to do` verbatim with `"No pull request was opened because this request asked for diagnosis only; <repo> is registered if you want a remediation PR."` and append a final `[next: open a remediation PR against <repo>]` line after the closing YAML fence (or `[next: open a remediation PR]` when `<repo>` would push the label past 75 characters, so Slack/Chat renders a clickable button outside the fold; omit `[next: ...]` under rules 1 and 2).
- **Required report layout for `kanban_complete.result` (and `send_notification` on background alerts)**: Use `"capacity constraint"` or `"scale-up failure"` — never `"stockout"`. Follow `SOUL.md` §7: bold Line 1 (~150 chars or fewer; omit location if unknown) naming what failed on `<workload_name>` (`<cluster_name>`) and the action needed, under 2,000 chars, naming the blocking node pool or `ComputeClass`, optional `[GKE Workloads Console](https://console.cloud.google.com/kubernetes/workload/overview?project=<project_id>)` (from `SOUL.md` §5), plain quota/capacity evidence (no rule codes or CLI dumps), and valid declarative YAML (never CLI commands inside a `yaml` block):

````markdown
**<workload_name> in <namespace> (<cluster_name>) cannot scale up; <recommended operator action>.**

## What's wrong

<pending pod impact, cluster/namespace, blocking node pool or ComputeClass>.

## Why

<plain-English autoscaler, quota, and capacity evidence without rule codes or CLI dumps> ([GKE Workloads Console](https://console.cloud.google.com/kubernetes/workload/overview?project=<project_id>)).

## What to do

<exact no-PR sentence from rule 1, 2, or 3 above> Apply this declarative configuration:

```yaml
# Remediation: <how this manifest change resolves the capacity constraint>
<concrete declarative ComputeClass, ContainerNodePool, or workload YAML manifest>
```
````

- **Avoid duplicate chat posts & finish via `kanban_complete`**:
  - **Interactive chat ask**: skip `send_notification` and put the formatted report above (opening `## What to do` with the exact matching sentence from rule 1, 2, or 3) in `kanban_complete(summary=..., result=...)`, then end without calling `kanban_block`.
  - **Background Pub/Sub alert**: call `send_notification(message=...)` (or `mcp_platform_control_send_notification`) with that formatted report if an outbound chat channel is enabled (if unavailable, proceed directly), then finish via `kanban_complete(summary=..., result=...)` without calling `kanban_block`.

### 6. Create GitOps Remediation Proposal (`MODE=gitops` only)

> [!CAUTION]
> Never use `execute_code` or Python subshells (see Step 1); run standard CLI commands directly one by one.

Do not modify the live GKE cluster directly. Propose the fix as a commit on the branch `prepare` checked out inside `<workspace>` (already on `platform-agent/remediate-stockout-<workload_name>`, so create no branch).

1. Apply fixes to the ComputeClass or workload YAML files **inside `<workspace>`**, appending an inline YAML comment (`# Remediation: ...`) to every changed/added line explaining how it mitigates the capacity constraint or scale-up failure.
2. **Self-Review**: Run `cd <workspace> && git diff` to confirm only strictly necessary changes are included and every changed YAML line carries `# Remediation: ...`.
3. **Major Changes / Migration (Rule E)**: Also write `<workspace>/docs/migrations/stockout-<workload_name>-plan.md` covering target region, resource copy strategy (DBs/storage/PVs), DNS/routing cutover, and rollout steps.
4. **PR Staging Hygiene**: Stage ONLY exact modified/created relative paths (`cd <workspace> && git add <path>`). **NEVER use `git add .`, `git add -A`, or `git commit -a`.**
5. Commit with a Conventional Commit message (e.g., `cd <workspace> && git commit -m "fix(compute-class): add fallback machine families to remediate capacity constraint"`).

### 7. Submit Suggestion & Open PR (`MODE=gitops` only)

**CRITICAL**: You MUST use the `submit_suggestion.py` helper script to open the Pull Request. Do NOT reach for a forge CLI. Do NOT write your own python script to create the PR.

**MANDATORY Summary Requirements**:

- 🛑 **NON-NEGOTIABLE RULE**: The description MUST contain the literal text `- **Checks Performed**:` followed by a `bash` code block containing the exact `kubectl describe pod ...`, `kubectl get configmap cluster-autoscaler-status -n kube-system -o yaml`, `kubectl get capacitybuffers.autoscaling.x-k8s.io -A`, `gcloud compute regions describe ...`, `gcloud beta compute advice capacity ...`, and `gcloud beta compute advice capacity-history ...` commands you executed during analysis. Failing to include that block will cause the PR to be rejected by automated SRE audit rules.
- Do NOT omit the `Checks Performed` section or its code block.
- Do NOT include any forge CLI commands in the summary or PR description. The `Checks Performed` block records diagnostics, not how the PR was opened.
- **Terminology**: Use `"capacity constraint"` or `"scale-up failure"` in the Pull Request title, description, YAML comments, and chat notifications — do NOT call the diagnosis a `"stockout"`.

Write the description to a file and pass the path, never `--body`. The `Checks Performed` block those rules mandate is nothing but backticks and `--format="json(...)"` strings, and inside the double quotes of a `--body` argument bash expands both — the diagnostic commands you are being told to record would run in the working copy instead of appearing in the pull request.

Run the `submit_suggestion.py` helper script with the `submit` subcommand to publish the branch and open a SRE review Pull Request EXACTLY as follows:

````bash
BODY=$(mktemp -p /opt/data/scratch pr_body.XXXXXX.md)
cat > "$BODY" <<'EOF'
### 🚨 Capacity & Scale-Up Diagnostic Report

- **Trigger**: Received scale-up failure alert for workload `<workload_name>` in cluster `<cluster_name>` (`<region_zone>`).
- **Diagnosis**: <detailed summary of what caused the capacity constraint or scale-up failure and which rule was violated>.
- **Checks Performed**:

```bash
# Diagnostic commands executed during analysis:
kubectl describe pod <pod_name> -n <namespace>
kubectl get configmap cluster-autoscaler-status -n kube-system -o yaml
kubectl get capacitybuffers.autoscaling.x-k8s.io -A
gcloud compute regions describe us-central1 --format="json(quotas.filter(metric=NVIDIA_L4_GPUS))"
gcloud beta compute advice capacity --provisioning-model=SPOT --instance-selection-machine-types="g2-standard-4,g2-standard-12" --target-distribution-shape=ANY --size=1 --region=us-central1 --format="json"
gcloud beta compute advice capacity-history --provisioning-model=SPOT --machine-type=g2-standard-4 --types=PREEMPTION,PRICE --region=us-central1 --format="json"
```

- **Remediation**: <description of the changes made to ComputeClass/workload manifests>.
EOF

EVIDENCE=$(mktemp -p /opt/data/scratch pr_evidence.XXXXXX.json)
cat > "$EVIDENCE" <<'EOF'
{
  "finding": "<the scale-up failure and the capacity constraint behind it>",
  "checks": [
    {"command": "kubectl get configmap cluster-autoscaler-status -n kube-system -o yaml", "output": "<the lines naming the failed scale-up>"},
    {"command": "gcloud beta compute advice capacity ...", "output": "<the lines showing the constraint>"}
  ],
  "change": "<what the ComputeClass/workload change does>",
  "verify": [
    {"command": "kubectl get pods -n <namespace> -l <selector>", "expect": "<pods Running on the new machine family>"}
  ]
}
EOF

python3 "$HERMES_HOME"/skills/submit-suggestion/scripts/submit_suggestion.py submit \
  --repo "<repo>" \
  --branch "platform-agent/remediate-stockout-<workload_name>" \
  --title "fix(capacity): remediate GKE capacity constraint for <workload_name>" \
  --body-file "$BODY" \
  --evidence-file "$EVIDENCE"
````

The evidence file follows **submit-suggestion** Step 3: commands you ran, each with the output lines that show the constraint, as JSON.
For `cluster-autoscaler-status`, quote only the `backoffInfo` and `scaleUp` lines, never the whole ConfigMap: the full `-o yaml` output is refused as secret data.

The quoted `<<'EOF'` matters as much as `--body-file`: unquoted, the heredoc expands the same constructs the argument would have.

`submit` finds the working copy `prepare` made, so there is nothing else from Step 3 to hand back — but `--branch` must be the branch you are standing on, and it refuses rather than guessing if the copy is on another one. Anything still uncommitted is recorded as one revision under `--title`. The script returns the live PR URL on stdout.

When running on a **background Pub/Sub alert** (skip `send_notification` on interactive chat asks, because `kanban_complete.result` already posts to the requester's thread), before finishing, call `send_notification` (or `mcp_platform_control_send_notification`) if an outbound chat channel is enabled to post a concise plain-English remediation summary linking `<PR_URL>` (omit `(<cluster_name>)` if unresolved; keep Line 1 plain bold text without an inline link so `kanban_complete.result` can reuse the same layout without stripping `<PR_URL>`, and do not paste raw CLI blocks):

```json
send_notification(message="**<workload_name> in <namespace> (<cluster_name>): opened a remediation PR.**\n\nPull request: [remediation PR](<PR_URL>).\n\n- **Summary**: <plain-English capacity constraint and manifest fix summary without CLI dumps>.")
```

After sending the notification (or skipping it on an interactive chat ask), return that same plain-English summary with the clickable `<PR_URL>` on line 2 via `kanban_complete`.
