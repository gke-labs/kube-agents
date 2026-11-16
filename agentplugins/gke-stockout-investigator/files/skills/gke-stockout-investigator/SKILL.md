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

Skip `send_notification` when responding to an interactive chat ask (the requester's thread receives live progress and `kanban_complete.result` automatically, so calling `send_notification` without `session_id` would duplicate into the home channel).

**CRITICAL**: For background Pub/Sub alerts, invoke `send_notification` (or `mcp_platform_control_send_notification`) as an actual tool call if an outbound channel is configured (do NOT output this notification only as plain markdown text), then proceed immediately to Step 2 in your next tool turn without stopping.

> [!IMPORTANT]
> When running in a background or PubSub context, NEVER use the `execute_code` tool or write Python scripts/subshells, as they trigger command approval safeguards and block indefinitely waiting for human approval. Only execute standard command-line tools directly (`kubectl`, `gh`, `gcloud`) or dedicated tools like `send_notification`.

### 2. Pre-Diagnosis Verifications (Duplicate PRs & False Signal Checks)

After sending the initial notification, perform two critical safety checks to see if you should stop immediately:

#### A. Check Operating Mode & Existing Pull Requests (Duplicate Prevention)

Determine whether this install has a usable GitOps repository configured (`MODE=gitops`) or is running without one (`MODE=chat-only`), and in GitOps mode inspect currently open Pull Requests in the repository:

1. Check registered GitOps repositories using `gitops_preflight.py` (which evaluates `/etc/gitops/managed_repos` and `context_repos` inside the sandbox; do **not** check again via shell file tests or `kubectl get configmap` on the GitOps state ConfigMap, because empty JSON arrays like `[]` or unusable entries are only diagnosed accurately by `gitops_preflight.py`, and `<agent>-shell` has no permission to read the ConfigMap):

   ```bash
   python3 "$HERMES_HOME"/scripts/gitops_preflight.py --list
   ```

   - **Exception to `SOUL.md` `NO_REGISTERED_REPO` stop**: For `gke-stockout-investigator` on background alerts or diagnostic/read-only asks (where the requester did not demand opening a PR), if `gitops_preflight.py --list` exits `3` (`NO_REGISTERED_REPO`, `MANAGED: none`, or no usable repository in `/etc/gitops/managed_repos`), do **NOT** call `kanban_block` and do **NOT** stop. Enter **Chat-Only Diagnostic Mode (`MODE=chat-only`)** instead: skip `vcs.py` and `submit_suggestion.py`, proceed directly to **Step 2 §B**, and stay strictly read-only — never modify, patch, apply, or delete cluster resources. If an interactive requester explicitly asked to open a PR, follow `SOUL.md`'s `NO_REGISTERED_REPO` rule (`kanban_block(kind="needs_input")`); on other preflight errors such as `STATE_UNREADABLE`, report via `kanban_block(kind="needs_input")`.
   - **If `MANAGED:` lists one or more repositories (exit `0`)**: stay in **GitOps Mode (`MODE=gitops`)**. Accept either `<owner>/<repo>` (GitHub) or `<host>/<path>` (self-managed GitHub Enterprise, GitLab, or Bitbucket — passing the exact value from `MANAGED:` as `--repo` on every `vcs.py` and `submit_suggestion.py` call; choose the target repository if multiple are returned). The pod is not a working copy (Step 3 explains why), so there is no local repository for a command to infer one from, and a call that omits `--repo` fails on repository resolution rather than returning a list — which is the duplicate check silently never happening.

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
   - For a background Pub/Sub alert where Step 1 posted a start notice and an outbound chat channel is enabled, send a brief plain-language stand-down via `send_notification` (`"**<workload_name> in <namespace> (<cluster_name>) already has an open [remediation PR #<number>](<url>); no new PR opened.**"` — omit `(<cluster_name>)` if unresolved; skip `send_notification` for interactive chat asks). Then call `kanban_complete` passing BOTH a short `summary` and a user-facing `result` opening with one bold sentence linking the PR: `"**<workload_name> in <namespace> (<cluster_name>) already has an open [remediation PR #<number>](<url>).**"` (omit `(<cluster_name>)` if unresolved).

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
5. **If and only if** the workload is confirmed healthy on `<cluster_context>` (zero `Pending` pods, all desired replicas `Running` and `Ready`) OR an alerted workload is absent across **every** non-excluded cluster with `fleet_scope().unread` empty and no `fleet_scope()` error:
   - If an interactive ask cannot locate the requested cluster/workload in scope — or if `fleet_scope().unread` is non-empty or returned an `ERROR:` leaving scope unverified — stop and call `kanban_block(kind="capability")` naming what could not be verified.
   - Otherwise (a recovered or absent background alert with verified scope): immediately STOP remediation. Do **NOT** call `kanban_block`, do not search a GitOps workspace, and do not propose changes.
   - For a background alert where Step 1 notified and outbound chat is enabled, call `send_notification`, then finish via `kanban_complete(summary=..., result=...)` (omit `(<cluster_name>)` if unresolved; never write `"false signal"` in `result`): for a healthy running workload, `"**<workload_name> in <namespace> (<cluster_name>) is healthy; no capacity remediation is needed.**\n\nAll desired pods are Running and Ready with zero Pending pods."`; for a workload absent across verified clusters, `"**<workload_name> in <namespace> was not found across verified clusters; no capacity remediation is needed.**"` (skip `send_notification` on interactive asks).

### 3. Parse Alert Details & Gather Manifests (`MODE=gitops` or `MODE=chat-only`)

If Step 2 checks pass (real active capacity constraint or scale-up failure, and no duplicate PR in GitOps mode):

1. **Parse details**: Extract the GKE cluster name and location (region/zone) from the alert details.
2. **Gather manifests based on your operating mode** (choose **Option A** in `MODE=gitops` or **Option B** in `MODE=chat-only`).

#### Option A: GitOps Mode (non-empty `MANAGED:` repository list)

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

**A repeat alert reuses the name.** It is one per workload, so an earlier
remediation pull request for this workload that was closed or squash-merged
left its branch behind. `prepare` deletes that spent branch itself and says
so in its log; nothing is needed from you.

**If `prepare` refuses the branch name**, it names the code. `NOT_SPENT`:
the old branch holds revisions no closed pull request carried.
`BRANCH_NOT_OURS`: the closed pull request on it was not this install's, or
it has carried a full page of pull requests, too many to read, or an open
pull request targets it. Treat those
revisions as somebody's. Report the refusal, its code and the branch,
and stop. `BRANCH_MOVED` (something pushed to it just now), `OPEN_PROPOSAL` (a pull
request was opened on it just now; the next run adds to it), and
`FORGE_CALL_FAILED`, `GIT_FAILED` or a refusal with no code (the delete did
not complete) all mean run `prepare` once more before reporting.
`FORGE_RATE_LIMITED` or `FORGE_UNAVAILABLE` (the forge turned the delete
away for now) means wait a few minutes, then run `prepare` once more.
Any other refusal: report it and stop. The helper's own message may suggest
another name; for this skill the name is fixed.

**Do not put a suffix on the name to get past the refusal.** The duplicate
check in Step 2 §A asks the forge about
`platform-agent/remediate-stockout-<workload>` and nothing else, so a pull request opened on any other name is invisible to the next
alert for this workload: that alert finds nothing open, reaches this same
refusal, suffixes again, and opens a second pull request for one incident.
One name per workload is what makes the duplicate check a check.

> [!CAUTION]
> **Every version-control command from here on runs inside the printed `workspace`, and through plain `git`.** The `git` on this machine holds no credential and cannot reach the network; use it for `add`, `commit`, `diff` and every other local verb. Do not call `/opt/vcs/libexec/git` directly — it is internal to that `git`. There is no shared clone to work in either — `/opt/data/workspace` and any other invented path will be rejected.

`prepare` has already brought the repository down and cut the branch from the repository's own default branch (`base`), so do **not** run a separate token refresh, `git checkout main`, `git pull` or `git checkout -b`. The copy has no remote to fetch from; the revisions go back up in Step 7.

3. **Search the workspace**: Locate the YAML manifests **inside the printed `workspace`** using targeted file searches (DO NOT use pattern `.*` or broad wildcard loops that paginate indefinitely):
   - For ComputeClass definitions, check `<workspace>/agents/platform/skills/gke-compute-classes/assets/` directly or use `search_files(pattern="compute-class")`.
   - For workload deployments, check `<workspace>/deployment/` or use `search_files(pattern="deployment")`.

#### Option B: Chat-Only Diagnostic Mode (no usable GitOps repository configured)

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

_CRITICAL MANDATE_: You MUST execute the quota check (`gcloud compute regions describe`), Spot capacity advice (`gcloud beta compute advice capacity`), capacity history (`gcloud beta compute advice capacity-history`), and attempt the autoscaler status check (`kubectl get configmap cluster-autoscaler-status`). In `MODE=gitops`, report ALL executed `gcloud` and `kubectl` diagnostic commands (including failed/denied attempts) in the Pull Request description (`- **Checks Performed**:`). In `MODE=chat-only`, do NOT paste raw `bash` blocks or CLI flags into `send_notification` or `kanban_complete` `result`; summarize the observed quota, capacity, and autoscaler facts in plain language instead.

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
> **MANDATORY PRIORITY CHECK**: If a ComputeClass `priorities[]` list contains more than 10 granular `machineType` rules (e.g., 25 priority rules for specific machine shapes like `n2-standard-4`, `n2-standard-8`, etc.), this is a Rule G violation. You MUST NOT add more `machineType` rules. Instead, you MUST auto-compress the configuration by replacing ALL 25 granular `machineType` rules with 4 family-level (`machineFamily`) rules (e.g., `n4`, `c3`, `n2`, `e2`).

- **Problem**: A Custom Compute Class (CCC) contains excessive granular `machineType` rules (e.g., 25 priority rules for specific machine shapes), exceeding Flex Advisor's cache limit (generating >200 combinations) and triggering a Cluster Autoscaler backoff reset loop. Lower-priority fallbacks (`n2`, `e2`) are starved and pods remain stuck in `Pending`.
- **Diagnosis**:
  1. **Static manifest check:** Inspect the `ComputeClass` manifest. If `spec.priorities` contains **more than 10 granular `machineType` rules**, or if total rule-to-zone permutations exceed Flex Advisor's 200-combination hard limit, flag as a Rule G violation. Note that `location.zones` is evaluated per priority rule (falling back to cluster default locations if omitted).
  2. **Runtime status check:** Inspect the `cluster-autoscaler-status` ConfigMap output (or recent pod events if RBAC-denied). Identify if the node groups created for fallback priorities (e.g., `n2`, `e2`) show continuous `backoffInfo` blocks or if they are repeatedly failing to scale up, while the primary node group is retried over a long duration (>5 minutes). This indicates a shared-backoff reset loop.
- **Fix**: Auto-compress the CCC configuration: Completely REPLACE the entire list of specific granular machine sizes (`machineType`) with 4 family-level definitions (`machineFamily`: `n4`, `c3`, `n2`, `e2`), reducing priority rules from 25 to 4 family-level priorities and avoiding the starvation loop.

#### Rule H: Hyperdisk Incompatibility with Older Generation Machines [ccc-hyperdisk-incompatible]

- **Problem**: A workload using Hyperdisk (e.g. `hyperdisk-balanced`, `hyperdisk-throughput`, `hyperdisk-extreme`, or StorageClass with hyperdisk CSI provisioner) uses a CCC definition whose 1st choice is a 3rd/4th generation machine type (e.g. `c3-standard-4`, `c4-standard-4`), but has fallbacks to older generation machine types (e.g. `c2`, `n2`, `e2`). Once the 1st choice is unavailable, Cluster Autoscaler falls back to an incompatible machine type (`c2`, `n2`, `e2`) that does not support Hyperdisk, causing scale-up to fail.
- **Diagnosis**:
  1. **Storage volume check:** Check if the workload uses Hyperdisk storage (e.g., look for `hyperdisk-balanced`, `hyperdisk-throughput`, or `hyperdisk-extreme` in the `StorageClass` or `Volume` definitions).
  2. **Incompatible fallback check:** Inspect the `ComputeClass` `priorities` list. If the workload uses Hyperdisk and the `priorities` list contains Gen 2 machine families (like `c2`, `n2`, or `e2`) as fallbacks, flag this as a Rule H violation.
- **Fix**: Increase CCC fallback options to other machine families compatible with Hyperdisk (e.g. `c3`, `c4`, `n4`, `c3d`), and remove fallbacks which do not work with Hyperdisk (`c2`, `n2`, `e2`).

### 6-Chat. Chat-Only Diagnostic Report (`MODE=chat-only` — No PR, Zero Writes)

If `MODE=chat-only` (because no usable GitOps repository is configured in `/etc/gitops/managed_repos`):

- **STOP before file edits, commits, or PR creation**: skip **Steps 6 and 7** completely. Do **NOT** invoke `submit_suggestion.py`, `vcs.py`, `git`, or any mutating `kubectl` / `gcloud` commands.
- **Terminology & Chat formatting (applies to `kanban_complete` `result` and `send_notification`)**: Use `"capacity constraint"` or `"scale-up failure"` in both `kanban_complete(result=...)` and any chat notification — do NOT call the diagnosis a `"stockout"`. Follow `SOUL.md` §7: open Line 1 with one concise bold sentence (~150 chars or fewer; omit location details if unknown) naming what failed on `<workload_name>` (`<cluster_name>`) and the action needed, keep notifications under 2,000 chars using `## What's wrong`, `## Why`, and `## What to do`, name the blocking node pool or `ComputeClass` plainly, optionally link `[GKE Workloads Console](https://console.cloud.google.com/kubernetes/workload/overview?project=<project_id>)` (from `SOUL.md` §5 when `<project_id>` is resolved; never guess other URLs), explain quota/capacity evidence in plain English (no rule codes or CLI dumps), state in prose outside code comments that no GitOps repository is configured, so no pull request was opened, and include valid declarative Kubernetes, Config Connector, or `ComputeClass` YAML (never CLI commands inside a `yaml` block).
- **Avoid duplicate chat posts**:
  - When investigating a background Pub/Sub alert (where no interactive chat user is waiting in a task thread), call `send_notification` (or `mcp_platform_control_send_notification`) if an outbound chat channel is enabled; if none is enabled or delivery is unavailable, proceed directly to `kanban_complete` without calling `kanban_block`:

````json
send_notification(message="**<workload_name> in <namespace> (<cluster_name>) cannot scale up; <recommended operator action>.**\n\n## What's wrong\n<pending pod impact, cluster/namespace, blocking node pool or ComputeClass>.\n\n## Why\n<plain-English autoscaler, quota, and capacity evidence without rule codes or CLI dumps> ([GKE Workloads Console](https://console.cloud.google.com/kubernetes/workload/overview?project=<project_id>)).\n\n## What to do\nNo GitOps repository is configured, so no pull request was opened. Apply this declarative configuration:\n```yaml\n# Remediation: <how this manifest change resolves the capacity constraint>\n<concrete declarative ComputeClass, ContainerNodePool, or workload YAML manifest>\n```")
````

- When responding to an interactive chat ask (where the requester's thread receives `kanban_complete.result` automatically), skip `send_notification` so you do not duplicate the report into the home channel.
- In both cases, finish by calling `kanban_complete` with BOTH a one-line `summary` AND the full formatted chat-only diagnostic report in `result` (matching every formatting requirement above: bold Line 1, plain-English quota/capacity evidence, blocking node pool or `ComputeClass`, declarative YAML fix, and explicit prose stating no GitOps repository is configured, so no pull request was opened), then end your turn without calling `kanban_block`.

### 6. Create GitOps Remediation Proposal (`MODE=gitops` only)

> [!CAUTION]
> **CRITICAL MANDATE: NEVER USE THE `execute_code` TOOL OR PYTHON SUBSHELLS.**
> In background/PubSub sessions, any invocation of `execute_code` (Python or bash script execution) triggers interactive command approval safeguards that will block and hang the session indefinitely. You MUST execute commands directly one by one using standard command-line tools or `run_command`, and use `send_notification` for alerts. Never write a Python script with `subprocess.run` to execute git or bash commands.

Do not modify the live GKE cluster directly. Instead, propose the change as a commit on the branch `prepare` already checked out for you.

Substitute `<workspace>` below with the exact path from Step 3's JSON line (e.g. `/opt/data/scratch/vcs/github__acme__fleet__platform-agent__remediate-stockout-frontend-web-app`). It is already on `platform-agent/remediate-stockout-<workload_name>`, so there is no branch to create.

1. Apply the fixes to the ComputeClass or workload YAML files **inside `<workspace>`**.
   - **Mandatory YAML Comments**: For EVERY change or addition in a YAML manifest (e.g. `topology.kubernetes.io/zone`, `nodeSelector`, `ComputeClass` priorities), append an inline YAML comment (`# Remediation: ...`) explaining how this specific change helps prevent or mitigate capacity constraints or scale-up failures.
2. **Self-Review Step**:
   - Run `cd <workspace> && git diff` to inspect all proposed changes before committing.
   - Verify that ONLY changes strictly necessary to mitigate the capacity constraint or scale-up failure are included (no unrelated formatting or whitespace edits).
   - Confirm that every updated YAML line includes the explanatory remediation comment.
3. **Special Case (Major Changes / Migration)**: If migrating to another region or changing architecture (Rule E), do NOT just change files. You **must** also write a detailed migration playbook in `<workspace>/docs/migrations/stockout-<workload_name>-plan.md`. This plan must detail:
   - Target destination region.
   - Resource copy strategy (DBs, storage, persistent volumes).
   - Network routing/DNS cutover approach.
   - Rollout steps.
4. **PR Staging Hygiene (MANDATORY)**: Stage ONLY the specific modified/created files using exact file paths relative to the repository root (e.g., `cd <workspace> && git add deployment/<workload_name>.yaml deployment/<compute_class_name>.yaml`). **NEVER use `git add .`, `git add -A`, or `git commit -a`**, as doing so will accidentally commit unrelated scratch files or workspace logs.
5. Commit using a Conventional Commit message (e.g., `cd <workspace> && git commit -m "fix(compute-class): add fallback machine families to remediate capacity constraint"`).

### 7. Submit Suggestion & Open PR (`MODE=gitops` only)

**CRITICAL**: You MUST use the `submit_suggestion.py` helper script to open the Pull Request. Do NOT reach for a forge CLI. Do NOT write your own python script to create the PR.

**MANDATORY Summary Requirements**:

- 🛑 **NON-NEGOTIABLE RULE**: The description MUST contain the literal text `- **Checks Performed**:` followed by a `bash` code block containing the exact `kubectl describe pod ...`, `kubectl get configmap cluster-autoscaler-status -n kube-system -o yaml`, `gcloud compute regions describe ...`, `gcloud beta compute advice capacity ...`, and `gcloud beta compute advice capacity-history ...` commands you executed during analysis. Failing to include that block will cause the PR to be rejected by automated SRE audit rules.
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

When running on a **background Pub/Sub alert** (skip `send_notification` on interactive chat asks, because `kanban_complete.result` already posts to the requester's thread), before finishing, call `send_notification` (or `mcp_platform_control_send_notification`) if an outbound chat channel is enabled to post a concise plain-English remediation summary linking `<PR_URL>` (do not paste raw CLI blocks):

```json
send_notification(message="**<workload_name> in <namespace> (<cluster_name>): opened [remediation PR](<PR_URL>).**\n\n- **Summary**: <plain-English capacity constraint and manifest fix summary without CLI dumps>.")
```

After sending the notification (or skipping it on an interactive chat ask), return the generated PR URL and a plain-English summary of your findings via `kanban_complete`.
