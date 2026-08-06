---
name: gke-stockout-investigator
description: Act upon GKE cluster-autoscaler stockout alerts, diagnose them using ComputeClass debugging guidelines, and submit a remediation Pull Request.
---

# GKE Stockout Diagnosis & GitOps Remediation Skill

This skill guides the Platform Agent on how to handle, diagnose, and remediate GKE capacity stockout alerts (`scale.up.error.out.of.resources` or equivalent) received via Google Cloud Pub/Sub.

## Workflow

### 1. Notify User of Investigation Start (MANDATORY FIRST STEP)

Before executing ANY terminal commands, scripts, or diagnostics, you MUST immediately call the `send_notification` tool (or `mcp_platform_control_send_notification`) to announce that the stockout alert was received and investigation is starting:

```json
send_notification(message="🚨 GKE Stockout Investigation Started\nWorkload: <workload_name>\nCluster: <cluster_name>\nDetails: A GKE capacity stockout alert is confirmed. I am starting an investigation and diagnosis.")
```

**CRITICAL**: You MUST invoke `send_notification` (or `mcp_platform_control_send_notification`) as an actual tool call. Do NOT output this notification as plain markdown text. After calling the tool, proceed immediately to Step 2 in your next tool turn without stopping.

> [!IMPORTANT]
> When running in a background or PubSub context, NEVER use the `execute_code` tool or write Python scripts/subshells, as they trigger command approval safeguards and block indefinitely waiting for human approval. Only execute standard command-line tools directly (`kubectl`, `gh`, `gcloud`) or dedicated tools like `send_notification`.

### 2. Pre-Diagnosis Verifications (Duplicate PRs & False Signal Checks)

After sending the initial notification, perform two critical safety checks to see if you should stop immediately:

#### A. Check for Existing Relevant Pull Requests (Duplicate Prevention)

To prevent duplicate effort and redundant PRs, inspect currently open Pull Requests in the repository:

1. Resolve the GitOps repository as `<owner>/<repo>`, and name it on every `gh` call from here on. The pod is not a git checkout (Step 3 explains why), so `gh` has no `origin` remote to infer a repository from: an unqualified `gh pr list` fails on repository resolution rather than returning a list, and the duplicate check silently never happens. The operator writes the repository into `SETTINGS.md` at provisioning time, so it is readable before anything has been cloned:

   ```bash
   grep -i "Git Repo:" "${PLATFORM_AGENT_HOME:-/opt/data}/SETTINGS.md"
   ```

   Strip any `https://github.com/` prefix and `.git` suffix from the value that line carries; what is left is `<owner>/<repo>`. Step 3 prints the same value as `repo`.

2. List all open PRs in that repository:
   ```bash
   gh pr list --repo <owner>/<repo> --state open --json number,title,headRefName,url
   ```
   If that call comes back unauthorized, refresh the GitHub App token once with `./scripts/github_token_refresh.py <owner>/<repo>` and retry. Do not refresh pre-emptively — the PR-creation flow in Step 3 mints its own token.
3. Extract the EXACT workload name from the alert payload (e.g., `frontend-web-app`, `ml-training-job-gpu`, `data-warehouse-analytics`, `llm-inference-service`). A PR is relevant ONLY IF:
   - The PR branch name (`headRefName`) contains `remediate-stockout-<exact_workload_name>`.
   - The PR title specifically names the `<exact_workload_name>`.
     **CRITICAL**: If an open PR exists for a DIFFERENT workload (e.g. `ml-training-job-gpu` when current alert is for `data-warehouse-analytics`), it is NOT a duplicate. Proceed with diagnosis and create a new PR for `<exact_workload_name>`.
4. **If a relevant PR is already open for THIS SPECIFIC workload**:
   - Immediately STOP processing.
   - Do NOT update, edit, modify, or rewrite the existing open PR description or contents.
   - Do NOT run any further diagnostics, do NOT search the workspace, do NOT create a new branch, and do NOT submit another PR.
   - Output a clear message to the user explaining that a relevant PR is already in place, referencing the PR number and URL (e.g. `Deduplicated: An open PR is already active for this stockout: PR #123 - https://github.com/<org>/<repo>/pull/123`).

#### B. Determine if the Stockout is a Real Issue or a False Signal

A stockout alert is a "false signal" if the cluster has already recovered (e.g. the unschedulable pods have been scheduled, deleted, or the issue was transient).

1. Identify the workload name and namespace from the alert payload (e.g., look at `jsonPayload.noDecisionStatus.noScaleUp.unhandledPodGroups` or the log text).
2. Check the current status of the pods for this workload in the namespace:
   ```bash
   kubectl get pods -n <namespace>
   ```
3. Check if there are any pods for this workload currently in `Pending` state with scheduling errors.
4. If there are pending pods, describe them to verify the event log:
   ```bash
   kubectl describe pod <pod_name> -n <namespace>
   ```
   Check if the events contain `FailedScheduling` with messages indicating lack of resources (e.g., "0/N nodes are available", "out of resources", "quota exceeded", "didn't match Pod's node affinity/selector") or `NotTriggerScaleUp` events. When pods use a custom `ComputeClass` or request GPUs, affinity/selector mismatches indicate that GKE cannot provision nodes matching the compute class due to zonal quota or capacity limits, and this MUST be treated as an active quota/stockout issue (not a false signal).
5. **If the workload pods do not exist in the namespace, OR if there are no pods for this workload currently in `Pending` state with scheduling errors (e.g. all pods are successfully `Running` or the workload is absent)**:
   - Identify this as a **false signal** or a transient issue that has already resolved.
   - Immediately STOP processing.
   - Do NOT run any further commands, do NOT search the workspace, and do NOT propose any configuration changes.
   - Output a clear message to the user explaining that the stockout is a false signal and the workload pods are currently healthy and running.

### 3. Parse Alert Details & Lease a GitOps Workspace

If the pre-diagnosis checks pass (no duplicate PRs and it is a real active stockout issue):

1. **Parse details**: Extract the GKE cluster name and location (region/zone) from the alert details.
2. **Lease a private workspace.** The pod is not a git checkout, and its volume is shared with every other agent running in it. `submit_suggestion.py prepare` clones the GitOps repository into a working tree that is yours alone, takes the remediation branch, and prints one JSON line:

   ```bash
   ./skills/submit-suggestion/scripts/submit_suggestion.py prepare \
     --branch "platform-agent/remediate-stockout-<workload_name>"
   ```

   ```json
   {
     "workspace": "/opt/data/gitops/t_9f3c1e07/acme__fleet",
     "lease": "t_9f3c1e07",
     "branch": "platform-agent/remediate-stockout-frontend-web-app",
     "base": "main",
     "repo": "acme/fleet",
     "started_from": "origin/main"
   }
   ```

   **Keep that whole line — Step 7 needs `workspace` and `lease` back.**

   > [!CAUTION]
   > **Every `git` command from here on runs inside the printed `workspace`, and nowhere else.** The credential proxy refuses `checkout`, `pull`, `add`, `commit`, `push` and every other tree-mutating verb outside a leased workspace, and the refusal is a security error rather than a retryable failure. There is no shared clone to work in: `/opt/data/workspace` and any other invented path will be rejected.

   `prepare` has already refreshed the git credentials, fetched the repository and cut the branch from the repository's own default branch (`base`), so do **not** run a separate token refresh, `git checkout main`, `git pull` or `git checkout -b`.

3. **Search the workspace**: Locate the YAML manifests **inside the printed `workspace`** using targeted file searches (DO NOT use pattern `.*` or broad wildcard loops that paginate indefinitely):
   - For ComputeClass definitions, check `<workspace>/agents/platform/skills/gke-compute-classes/assets/` directly or use `search_files(pattern="compute-class")`.
   - For workload deployments, check `<workspace>/deployment/` or use `search_files(pattern="deployment")`.

### 4. Diagnose Capacity, Quotas, and Resource Usage

**Efficiency Directive**: Execute diagnostic commands efficiently. Combine checks into a single step where possible. Do not spend excessive turns on repetitive queries. Once diagnostics are gathered, proceed immediately to self-review and PR creation using `submit_suggestion.py`.

Before proposing any configuration changes (e.g., adding fallbacks, shrinking VM shapes, or reserving resources), execute the following diagnostic commands to check GCP quotas, reservations, actual workload resource utilization, and Spot VM availability advice.

#### A. Quota Verification

Verify that the proposed machine families, CPU, or GPU metric counts are within the region's quota limits:

```bash
gcloud compute regions describe us-central1 --format="json(quotas.filter(metric=CPUS))"
gcloud compute regions describe us-central1 --format="json(quotas.filter(metric=NVIDIA_L4_GPUS))"
```

_Note: Filter by other metric names (e.g., `N4_CPUS`, `C4_CPUS`, `NVIDIA_T4_GPUS`, `NVIDIA_A100_GPUS`) to inspect specific hardware._

#### B. Reservations Check

Check if any zonal reservations are available for the target workload's machine type to guarantee compute capacity:

```bash
gcloud compute reservations list --format="json"
```

#### C. Actual Workload Resource Usage

Before proposing resource reservations or changing VM shapes, analyze actual usage and account for potential spikes. Use:

```bash
# Get node CPU/memory utilization summary
kubectl top node

# Fetch raw metrics from the metrics API server
kubectl get --raw "/apis/metrics.k8s.io/v1beta1/nodes"

# Get pod CPU/memory utilization summary
kubectl top pod -n <namespace>
```

#### D. Spot VM Availability and Pricing Advice

If configuring fallback Spot instances or diagnosing GPU stockouts, use the Spot advice APIs to check obtainability and preemption risk across target zones:

1. **VM & GPU Availability Advice**:
   ```bash
   gcloud beta compute advice capacity \
       --provisioning-model=SPOT \
       --instance-selection-machine-types="g2-standard-4,g2-standard-12,n1-standard-4" \
       --target-distribution-shape=ANY \
       --size=1 \
       --region=us-central1 \
       --format="json"
   ```
2. **Preemption Rate and Price History**:
   ```bash
   gcloud beta compute advice capacity-history \
       --provisioning-model=SPOT \
       --machine-type=g2-standard-4 \
       --types=PREEMPTION,PRICE \
       --region=us-central1 \
       --format="json"
   ```

_CRITICAL MANDATE_: You MUST execute the quota check (`gcloud compute regions describe`), Spot capacity advice (`gcloud beta compute advice capacity`), and capacity history (`gcloud beta compute advice capacity-history`), and report ALL executed `gcloud` and `kubectl` diagnostic commands in BOTH the chat notification (`send_notification`) and the Pull Request description.

### 5. Diagnose Using ComputeClass Debugging Guidelines

Inspect the target `ComputeClass` and workload manifests in the leased workspace, checking against the following debugging rules:

#### Rule A: Lack of Zone/Family Fallbacks

- **Problem**: The ComputeClass `priorities[]` is pinned to a single machine family or a single zone, leaving no alternative when GCE encounters a stockout.
- **Fix**: Propose adding fallback priorities (additional machine families like `n4`, `c4`, `n2` or other zones within the region).

#### Rule B: Large VM Shape Scarcity (>32 vCPUs)

- **Problem**: The workload requests very large VMs (>32 vCPU) which draw from thinner capacity pools and are highly prone to stockouts.
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

- **Problem**: The requested specialized hardware (e.g., Nvidia H100, L4, or TPU v5e) is completely stocked out across all zones in the target region.
- **Fix**: Recommend migrating the workload and its infrastructure to another GCP region where capacity is available, or changing the application architecture to use a more available hardware class.

#### Rule F: Regional Quota Exceeded Violation (`quota exceeded` / GPU Limit Cap)

- **Problem**: A workload requests more total resources (CPUs or GPUs) than the regional quota limit configured for the project in that region (e.g., requesting 32 L4 GPUs when `gcloud compute regions describe us-central1` shows the `NVIDIA_L4_GPUS` quota limit is 24).
- **Fix**: Identify this explicitly as a **Regional Quota Exceeded Violation** in the diagnosis. Propose adjusting the workload deployment manifest to cap total requested GPUs/CPUs to fit strictly within the regional quota limit (e.g. reducing replicas from 4 to 3 so total GPUs = 24), and create a `ComputeClass` providing multi-zone fallback capabilities.

#### Rule G: CCC Priority Starvation & Reset Loop (Excessive Granular Machine Types)

> [!IMPORTANT]
> **MANDATORY PRIORITY CHECK**: If a ComputeClass `priorities[]` list contains more than 10 granular `machineType` rules (e.g., 25 priority rules for specific machine shapes like `n2-standard-4`, `n2-standard-8`, etc.), this is a Rule G violation. You MUST NOT add more `machineType` rules. Instead, you MUST auto-compress the configuration by replacing ALL 25 granular `machineType` rules with 4 family-level (`machineFamily`) rules (e.g., `n4`, `c3`, `n2`, `e2`).

- **Problem**: A Custom Compute Class (CCC) contains excessive granular `machineType` rules (e.g., 25 priority rules for specific machine shapes), exceeding Flex Advisor's cache limit (generating >200 combinations) and triggering a Cluster Autoscaler backoff reset loop. Lower-priority fallbacks (`n2`, `e2`) are starved and pods remain stuck in `Pending`.
- **Fix**: Auto-compress the CCC configuration: Completely REPLACE the entire list of specific granular machine sizes (`machineType`) with 4 family-level definitions (`machineFamily`: `n4`, `c3`, `n2`, `e2`), reducing priority rules from 25 to 4 family-level priorities and avoiding the starvation loop.

#### Rule H: Hyperdisk Incompatibility with Older Generation Machines

- **Problem**: A workload using Hyperdisk (e.g. `hyperdisk-balanced`, `hyperdisk-throughput`, `hyperdisk-extreme`, or StorageClass with hyperdisk CSI provisioner) uses a CCC definition whose 1st choice is a 3rd/4th generation machine type (e.g. `c3-standard-4`, `c4-standard-4`), but has fallbacks to older generation machine types (e.g. `c2`, `n2`, `e2`). Once there is a stockout on the 1st choice, Cluster Autoscaler falls back to an incompatible machine type (`c2`, `n2`, `e2`) that does not support Hyperdisk, causing scale-up to fail.
- **Fix**: Increase CCC fallback options to other machine families compatible with Hyperdisk (e.g. `c3`, `c4`, `n4`, `c3d`), and remove fallbacks which do not work with Hyperdisk (`c2`, `n2`, `e2`).

### 6. Create GitOps Remediation Proposal

> [!CAUTION]
> **CRITICAL MANDATE: NEVER USE THE `execute_code` TOOL OR PYTHON SUBSHELLS.**
> In background/PubSub sessions, any invocation of `execute_code` (Python or bash script execution) triggers interactive command approval safeguards that will block and hang the session indefinitely. You MUST execute commands directly one by one using standard command-line tools or `run_command`, and use `send_notification` for alerts. Never write a Python script with `subprocess.run` to execute git or bash commands.

Do not modify the live GKE cluster directly. Instead, propose the change as a commit on the branch `prepare` already checked out for you.

Substitute `<workspace>` below with the exact path from Step 3's JSON line (e.g. `/opt/data/gitops/t_9f3c1e07/acme__fleet`). It is already on `platform-agent/remediate-stockout-<workload_name>`, so there is no branch to create.

1. Apply the fixes to the ComputeClass or workload YAML files **inside `<workspace>`**.
   - **Mandatory YAML Comments**: For EVERY change or addition in a YAML manifest (e.g. `topology.kubernetes.io/zone`, `nodeSelector`, `ComputeClass` priorities), append an inline YAML comment (`# Remediation: ...`) explaining how this specific change helps prevent or mitigate stockouts.
2. **Self-Review Step**:
   - Run `cd <workspace> && git diff` to inspect all proposed changes before committing.
   - Verify that ONLY changes strictly necessary to mitigate the stockout are included (no unrelated formatting or whitespace edits).
   - Confirm that every updated YAML line includes the explanatory remediation comment.
3. **Special Case (Major Changes / Migration)**: If migrating to another region or changing architecture (Rule E), do NOT just change files. You **must** also write a detailed migration playbook in `<workspace>/docs/migrations/stockout-<workload_name>-plan.md`. This plan must detail:
   - Target destination region.
   - Resource copy strategy (DBs, storage, persistent volumes).
   - Network routing/DNS cutover approach.
   - Rollout steps.
4. **PR Staging Hygiene (MANDATORY)**: Stage ONLY the specific modified/created files using exact file paths relative to the repository root (e.g., `cd <workspace> && git add deployment/<workload_name>.yaml deployment/<compute_class_name>.yaml`). **NEVER use `git add .`, `git add -A`, or `git commit -a`**, as doing so will accidentally commit unrelated scratch files or workspace logs.
5. Commit using a Conventional Commit message (e.g., `cd <workspace> && git commit -m "fix(compute-class): add fallback machine families to remediate stockout"`).

### 7. Submit Suggestion & Open PR

**CRITICAL**: You MUST use the `submit_suggestion.py` helper script to open the Pull Request. Do NOT use `gh pr create` directly. Do NOT write your own python script to create the PR.

**MANDATORY Summary Requirements**:

- 🛑 **NON-NEGOTIABLE RULE**: The `--body` string for `submit_suggestion.py` MUST contain the literal text `- **Checks Performed**:` followed by a `\`\`\`bash`code block containing the exact`kubectl describe pod ...`, `gcloud compute regions describe ...`, `gcloud beta compute advice capacity ...`, and `gcloud beta compute advice capacity-history ...`commands you executed during analysis. Failing to include this`\`\`\`bash`block in the`--body` argument will cause the PR to be rejected by automated SRE audit rules.
- Do NOT omit the `Checks Performed` section or code block from the `--body` argument.
- Do NOT include any `gh` commands (such as `gh pr list` or `gh pr create`) in the summary or PR description.

Run the `submit_suggestion.py` helper script with the `submit` subcommand to push the branch and open a SRE review Pull Request EXACTLY as follows, substituting `<workspace>` and `<lease>` with the values Step 3 printed:

```bash
./skills/submit-suggestion/scripts/submit_suggestion.py submit \
  --workspace "<workspace>" \
  --lease "<lease>" \
  --branch "platform-agent/remediate-stockout-<workload_name>" \
  --title "fix(capacity): remediate GKE stockout for <workload_name>" \
  --body "### 🚨 Stockout Diagnostic Report

- **Trigger**: Received stockout alert for workload \`<workload_name>\` in cluster \`<cluster_name>\` (\`<region_zone>\`).
- **Diagnosis**: <detailed summary of what caused the stockout and which rule was violated>.
- **Checks Performed**:
\`\`\`bash
# Diagnostic commands executed during analysis:
kubectl describe pod <pod_name> -n <namespace>
gcloud compute regions describe us-central1 --format=\"json(quotas.filter(metric=NVIDIA_L4_GPUS))\"
gcloud beta compute advice capacity --provisioning-model=SPOT --instance-selection-machine-types=\"g2-standard-4,g2-standard-12\" --target-distribution-shape=ANY --size=1 --region=us-central1 --format=\"json\"
gcloud beta compute advice capacity-history --provisioning-model=SPOT --machine-type=g2-standard-4 --types=PREEMPTION,PRICE --region=us-central1 --format=\"json\"
\`\`\`
- **Remediation**: <description of the changes made to ComputeClass/workload manifests>.
"
```

`--workspace` and `--lease` are not optional bookkeeping. `prepare` and `submit` are separate processes: omit `--workspace` and `submit` falls back to the current directory, which holds no lease; omit `--lease` and it has no lease to check the tree against. Either way it stops with a `PermissionError` instead of opening the PR. The script returns the live GitHub PR URL on stdout.

When running in a background/PubSub context or when a new SRE review Pull Request with remediation is being created, before providing your final response, you MUST call the `send_notification` tool to notify the user/SRE immediately (do not run any scripts or external RPC clients):

````json
send_notification(message="🛠️ GKE Stockout Remediation Proposed\nWorkload: <workload_name>\nPR: <PR_URL>\nSummary: <summary>\nChecks Performed:\n```bash\nkubectl describe pod <pod_name>\ngcloud compute regions describe us-central1 ...\ngcloud beta compute advice capacity ...\ngcloud beta compute advice capacity-history ...\n```")
````

After calling the tool, provide the user with the generated PR URL and a summary of your findings.
