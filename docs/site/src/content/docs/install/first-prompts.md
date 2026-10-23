---
title: First prompts
description: The first message to send a new install, what a healthy answer looks like and how long it takes, and five things to ask the agent next.
---

Once the [quick start](/kube-agents/install/quickstart-gke/) shows the `PlatformAgent` `Ready`, the install is checked by talking to it. Send the messages below as chat messages to the app, in a Slack DM or a Google Chat space. Under `spec.mode: today`, when no home channel was set at install, the chat the first message comes from becomes the home channel, where scheduled reports and alerts post; send it from the space the team watches, or run `/sethome` there afterwards. Under `next` the first message sets no home channel, except for Slack when both platforms are enabled; [First-run onboarding](/kube-agents/concepts/chatops/#first-run-onboarding) has the rule. An install with no chat platform reaches the agent over `kubectl exec` instead; [When no chat platform is enabled](/kube-agents/concepts/chatops/#when-no-chat-platform-is-enabled) gives the command and what that lane does not exercise.

## The first message: which clusters, and can it reach them

```text
Which GKE clusters do you manage, and can you reach each one?
```

This is the install check. The answer shows that the agent has the scope it was given and read access to every cluster in it. It does not show whether each cluster has a Cluster Agent profile; the roster is covered below, under the five prompts.

What the thread shows, in order:

1. **A greeting around the answer.** The first message anyone sends a new install is also its onboarding: the agent answers the ask, then introduces itself in two sentences. Later, when the first-run inventory scan has finished, its ranked report is posted into this same thread. [First-run onboarding](/kube-agents/concepts/chatops/#first-run-onboarding) describes each stage.
2. **One short line naming what is being checked**, such as `checking all 5 clusters.` The Planning Agent has filed the work as a card for the Platform Agent; [how a chat question is answered under `next`](/kube-agents/concepts/chatops/#how-a-chat-question-is-answered-under-next) walks through the status line and the card.
3. **The result, as its own message.** One bold sentence gives the verdict, then one row per cluster: name, location, status, whether the agent could reach it, control-plane version, node count. On a one-cluster install the table has one row, the cluster kube-agents runs on, because the scope includes the management cluster unless the `PlatformAgent` excludes it.

An answer measured on a five-cluster install, with the cluster and project names replaced and two of its five rows shown:

> **All 5 managed GKE clusters in project `<project>` are in RUNNING status and fully reachable via their Kubernetes control-plane APIs.**
>
> Every cluster registered under the declared fleet scope responded successfully to Kubernetes version queries and resource inspections. Across the inventory, 4 clusters run control-plane version `1.35.8-gke.1439001` while `cluster-b` runs `1.34.12-gke.1011000`.
>
> | Cluster     | Location        | Status    | Reachability | Version               | Nodes |
> | ----------- | --------------- | --------- | ------------ | --------------------- | ----- |
> | `cluster-a` | `us-east4`      | `RUNNING` | Reachable    | `1.35.8-gke.1439001`  | 6     |
> | `cluster-b` | `us-central1-a` | `RUNNING` | Reachable    | `1.34.12-gke.1011000` | 2     |

Google Chat does not render a pipe table, so the verdict sentence and the sentences under it carry the facts; the card format asks the agent to leave no fact in the table alone. How Slack shows a table depends on the mode; [Chat platforms](/kube-agents/concepts/chatops/) says which path each mode uses.

To produce it the Platform Agent read the declared scope and the Cluster Agent roster, listed the clusters, and queried each one's Kubernetes version. On that run, with Gemini 3.8 Flash as the model, the acknowledgement arrived after 21 seconds and the result after a further 6.6 minutes. The acknowledgement is the short Planning Agent turn that files the card and replies, and it took between 21 seconds and 15 minutes across six prompts on that install while the model was slow; the result takes longer on a larger fleet. A long silence is therefore usually the model, not the install. Under `spec.mode: next`, the install is at fault when the task was never taken: on Google Chat the status line stays at `⏳ submitted…`, and on Slack, which has no status line, the bridge log shows no `task accepted` for the message. For that case, [A Question Gets No Answer Under `spec.mode: next`](https://github.com/gke-labs/kube-agents/blob/main/INSTALL.md#7-a-question-gets-no-answer-under-specmode-next) is the troubleshooting entry.

A declared project the agent cannot list yet drops out of the table whole. On a first install that is usually the read grant still propagating; the hourly reconcile adds the project's clusters once the grant lands. `fleet_scope.json`, beside the profiles on the data volume, records each project's outcome (`denied`, `api-disabled`, `unreachable`, `over-cap`); the [`spec.scope`](/kube-agents/operator/platformagent-crd/#specscope) section explains them. A cluster missing while its project is listed is outside the declared scope: `spec.scope` names the projects and the exclusions. A cluster listed as not reachable is an access problem: [Security and IAM](/kube-agents/reference/security-and-iam/) is canonical for what the agent's identity may read.

## Five things to ask it

Each of these is a single chat message. The agent answers in the thread from read-only reads of the fleet, the GCP projects it monitors, and the linked GitOps repositories, and it recommends changes without applying them.

1. **"Which clusters are behind their release channel, and what would block upgrading them?"** — Every cluster's control-plane and node-pool versions against the channel default, with what would stop the upgrade named: drain-blocking PodDisruptionBudgets, fail-closed admission webhooks with an unreachable backend in the upgrade's path or able to refuse the control plane's bootstrap RBAC writes (ClusterRoles and ClusterRoleBindings; Roles and RoleBindings in kube-system and kube-public), maintenance exclusions, node-pool version skew, and the blockers the per-entry rules of the upgrade-failure catalogue file, with the risks that do not block listed beside them. Name a target version and it also scans the GitOps manifests for the `apiVersions` that version removes. ([`fleet-upgrade-verification`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/skills/fleet-upgrade-verification/SKILL.md))
2. **"Which workloads request far more CPU and memory than they use?"** — One run of the fleet waste audit's collector compares a week of Cloud Monitoring peaks with each workload's requests, so the answer names the over-requested controllers in resource units, beside idle pools and orphaned disks; a stock install has no billing export to price them. ([`fleet_wide_cost_analysis_sop.md`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/governance/fleet_wide_cost_analysis_sop.md))
3. **"Design a Standard cluster for 32 A100s in us-central1. Check quota and live obtainability, and give me a ComputeClass fallback. Design only."** — Quota and capacity are checked separately, each against live `gcloud` evidence, and the design names the fallback tiers that change the workload's GPU class or interconnect characteristics. ([`capacity-obtainability`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/skills/capacity-obtainability/SKILL.md), [`gke-compute-classes`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/skills/gke-compute-classes/SKILL.md))
4. **"Something in the `payments` namespace on `prod-east` stopped making progress without erroring. Find it."** — The work is delegated to the Cluster Agent for that cluster, which looks for controllers whose `observedGeneration` lags, progress conditions that stopped advancing, repeating warning events, and references to objects that do not exist. ([`gke-stall-detection`](https://github.com/gke-labs/kube-agents/blob/main/agents/cluster/skills/gke-stall-detection/SKILL.md))
5. **"`checkout` on `prod-east` has been crash-looping since this morning. What happened?"** — The Cluster Agent for that cluster fixes the time window, reads the container's exit codes and the events around them, tells an OOM kill from an application crash, and proposes the manifest correction without applying it. ([`gke-workload-troubleshooting`](https://github.com/gke-labs/kube-agents/blob/main/agents/cluster/skills/gke-workload-troubleshooting/SKILL.md))

In the last two, replace `prod-east`, `payments` and `checkout` with a cluster name from the first answer and a namespace and workload on it. A named cluster's question goes to that cluster's Cluster Agent once its profile exists. The first-run onboarding creates a profile for every cluster the agent can list within minutes of the install; a scoped project whose read grant has not propagated gets its profiles from the hourly reconcile, which also keeps the roster current. For a cluster that has no profile yet, the Platform Agent answers it ([`cluster-agent-lifecycle`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/skills/cluster-agent-lifecycle/SKILL.md)).

On an install running `spec.mode: next`, the agent still answers these in the thread, with a few differences in how the answer is delivered; [how a chat question is answered under `next`](/kube-agents/concepts/chatops/#how-a-chat-question-is-answered-under-next) walks through each step.

## Where to go next

- [Skill catalog](/kube-agents/skills/) — everything the agent can do out of the box.
- [Autonomous watchdogs](/kube-agents/concepts/autonomous-watchdogs/) — the audits that run on a schedule without being asked.
