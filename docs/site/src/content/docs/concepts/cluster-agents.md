---
title: Cluster Agents
description: Read-only per-cluster specialists the Platform Agent creates on demand and delegates single-cluster runtime debugging to.
sidebar:
  order: 2
---

A **Cluster Agent** is a read-only SRE scoped to exactly one GKE cluster. It is a Hermes profile (`cluster-<project>-<cluster>-<location>`, sanitized to 63 characters) that the [Platform Agent](/kube-agents/concepts/platform-agent/) scaffolds at runtime inside its own pod — one per managed cluster, persisting on the data volume until that cluster is deleted. `SOUL.md §6` makes delegation the default: work scoped to a single cluster's live runtime (crash loops, OOMs, scheduling failures, mount errors, connectivity, autoscaling, storage, observability gaps) belongs to that cluster's Cluster Agent, while fleet-wide audits, provisioning, and the GitOps write path stay with the Platform Agent.

## Scoped by construction

Each profile is stamped from the [`agents/cluster/`](https://github.com/gke-labs/kube-agents/tree/main/agents/cluster) template (baked into the image at `/opt/cluster-template`) by [`cluster_agent_profile.py`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/scripts/cluster_agent_profile.py):

- **One cluster only.** A `KUBECONFIG` pinned to the target cluster is written into the profile's `.env`, and the cluster's project/name/location are recorded as a `cluster_identity` block in its config.
- **Read-only toolset.** The template config exposes only the `gke` and `developer_knowledge` MCP servers — no `platform_control` (provisioning), no `agent_common` (delegation), no GitOps write path. A Cluster Agent diagnoses; it never mutates cluster state and never opens pull requests.
- **Its own skills.** Six single-cluster runtime-debugging skills ship in [`agents/cluster/skills/`](https://github.com/gke-labs/kube-agents/tree/main/agents/cluster/skills) (observability, reliability, storage, workload scaling, workload security, workload troubleshooting) — listed under their own heading in the [skill catalog](/kube-agents/skills/).

## Lifecycle

A managed cluster and its Cluster Agent profile are created together and deleted together (`SOUL.md §6`). Three paths maintain that invariant:

1. **Onboarding.** When the Platform Agent provisions a cluster (`gke-cluster-creator`) or first brings one under management, the `cluster-agent-lifecycle` skill creates the profile.
2. **On request.** "Manage my cluster `X` in `Y`" invokes the `manage-cluster` skill, which verifies the cluster exists and creates its profile (idempotent).
3. **Reconciliation.** The hourly `cluster-agent-reconcile` job (a `no_agent` script job on the Chat Agent profile's [cron file](/kube-agents/reference/cron-jobs/)) sweeps the project: it creates a profile for every cluster that lacks one — excluding the management cluster kube-agents itself runs on, which it identifies via the GKE metadata server — and prunes a profile only when its cluster is _definitively_ gone (a NotFound from `gcloud container clusters describe`). Ambiguous errors (auth, network, quota) never trigger deletion.

## How delegation works

Delegation runs on the shared kanban board — agents never pass context to each other directly:

1. The Platform Agent resolves the cluster's profile name (`cluster_agent_profile.py name ...`) and files a card: `kanban_create(assignee="<profile>", body="<namespace/workload, symptom, time window>")`.
2. The gateway's dispatcher auto-spawns the Cluster Agent as a worker on that card; `kanban_notify_propagate.py` copies the chat subscription onto it so the user sees the cluster's progress in the thread.
3. The worker completes the card with a structured `metadata` handoff — a grounded root-cause analysis plus a proposed manifest patch.
4. The Platform Agent reads the result and decides whether to submit the fix through the [declarative workflow](/kube-agents/concepts/declarative-workflow/) (`submit-suggestion`). The write path never moves to the cluster side.

For multi-cluster work the Platform Agent fans out one card per cluster plus a fan-in card assigned to itself, synthesizing every parent's `metadata` once all complete — see the `workload-rebalancing` skill for the pattern.

## Security posture

A Cluster Agent shares the pod's identity — the same KSA and GSA, so the same enforced IAM and RBAC ceilings apply ([Security &amp; IAM](/kube-agents/reference/security-and-iam/)). The profile split is a scoping-down inside those ceilings: fewer MCP servers, no write skills, one pinned cluster context.

## Where to go next

- [Platform Agent](/kube-agents/concepts/platform-agent/) — the orchestrator that owns Cluster Agent lifecycle and the write path.
- [Architecture](/kube-agents/overview/architecture/) — where the `cluster-*` profiles sit in the pod.
- [Skill catalog](/kube-agents/skills/) — the per-cluster runtime skill group.
