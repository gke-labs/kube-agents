---
name: cluster-agent-lifecycle
description: Create, delegate to, and tear down per-cluster Cluster Agent Hermes profiles. Use whenever a GKE cluster is onboarded or deleted, or whenever a single-cluster runtime debugging/operations task should be delegated to that cluster's Cluster Agent.
---

# Cluster Agent Lifecycle Skill

As the Platform Agent you own the lifecycle of **Cluster Agents**. A Cluster Agent is a Hermes _profile_ — an isolated agent instance with its own persona (`SOUL.md`), scoped toolset, and home directory — that you create dynamically **inside your own pod**, one per managed GKE cluster. It handles read-only runtime operations and deep workload diagnostics on that single cluster, and returns its findings to you.

You never debug tenant workloads directly. You delegate that to the cluster's Cluster Agent and act on what it returns.

The engine for all of this is the helper script `scripts/cluster_agent_profile.py` (resolved at `/opt/data/scripts/cluster_agent_profile.py` at runtime).

## When to create a profile

Create the Cluster Agent profile as part of **cluster onboarding** — immediately after a cluster is successfully provisioned (see `gke-cluster-creator`) or when an existing cluster is first brought under management (see `manage-cluster`). A managed cluster and its Cluster Agent profile are created together: never leave a managed cluster without a profile.

```bash
python3 /opt/data/scripts/cluster_agent_profile.py create \
  --project "<project>" --cluster "<cluster>" --location "<location>"
```

This scaffolds the profile home on the persistent data PVC, pins a kubeconfig scoped to that cluster, writes the cluster identity into the profile's `USER.md`, and registers the profile. It is **idempotent** — safe to re-run. It prints the profile name.

## How to delegate a debugging / runtime-ops task (kanban board)

For any request that concerns runtime behavior of workloads on a **single, specific** cluster (crash loops, OOMs, scheduling failures, mount errors, connectivity, autoscaling, storage, observability gaps), delegate to that cluster's Cluster Agent instead of investigating yourself.

**Personas never pass context directly.** Delegation runs on the shared **kanban board**: you create a card assigned to the cluster's profile; the gateway's kanban dispatcher **auto-spawns** the Cluster Agent to work it; it reports a structured result on the card. You do **not** invoke the agent yourself.

1. **Resolve the cluster's profile name** (the kanban `assignee`):

   ```bash
   python3 /opt/data/scripts/cluster_agent_profile.py name \
     --project "<project>" --cluster "<cluster>" --location "<location>"
   ```

2. **Create the card** with the request in the body:

   ```
   kanban_create(
     assignee="<profile-name>",
     title="<short title>",
     body="<full request: namespace/workload, symptom, time window>"
   )
   ```

   The dispatcher spawns the Cluster Agent (`hermes -p <profile> chat -q "work kanban task <id>"`) automatically; it reads the card, does read-only diagnostics, and calls `kanban_complete(summary=..., metadata={...})`.

3. **Read the result** — you are auto-subscribed, so the completion (or a `needs_input` block) is pushed into your chat. You can also inspect it: `kanban_show(<id>)`. The RCA and any proposed patch are in the card's `metadata`, not the worker's chat reply.

**Multi-cluster (fan-out / fan-in):** create one card per cluster (parents), plus a card **assigned to yourself** with `parents=[<parent ids>]` (the fan-in child). Once all parents complete, the dispatcher spawns you on the child card, whose context includes every parent's `metadata`. See the **`workload-rebalancing`** skill for the validation-then-declare pattern.

## Acting on the result

The Cluster Agent is **read-only** and does not open Pull Requests. After reading the completed card:

1. Review the RCA and proposed manifest patch in the card's `metadata`.
2. If a change is warranted, **you** open (or update) the Pull Request via the `submit-suggestion` skill — you own the GitOps write path. Reconcile against any existing branch/PR for the same workload before creating a new one.
3. Report the outcome to the user as a clean SRE status update.

## When to delete a profile

Delete the Cluster Agent profile as part of **cluster teardown** (see `gke-cluster-lifecycle`), after the cluster itself is removed:

```bash
python3 /opt/data/scripts/cluster_agent_profile.py delete \
  --project "<project>" --cluster "<cluster>" --location "<location>"
```

This deregisters the profile and removes its home directory. Do not delete a profile while its cluster still exists.

## Automatic reconciliation (orphan pruning)

Profiles are also pruned automatically. An hourly, deterministic `no_agent` cron job
(`cluster-agent-reconcile`) runs `scripts/cluster_agent_reconcile.py`, which enumerates the managed
Cluster Agent profiles, reads each one's `cluster_identity`, and **deletes any profile whose GKE
cluster no longer exists**. This closes the loop when a cluster is deleted out-of-band (so its
profile is never left orphaned pointing at a dead kubeconfig).

It is conservative by design: a profile is removed **only** on a definitive GKE `NotFound`. Any
inconclusive check (auth/network/timeout, or a missing `cluster_identity`) leaves the profile
untouched. When it prunes anything it posts a Google Chat summary.

To preview what would be pruned without deleting anything:

```bash
python3 /opt/data/scripts/cluster_agent_reconcile.py --dry-run
```

You still delete a profile explicitly during planned teardown (above) — reconciliation is the safety
net, not the primary path.

## Listing profiles

```bash
python3 /opt/data/scripts/cluster_agent_profile.py list
```

Lists the currently provisioned Cluster Agent profiles (one per managed cluster).
