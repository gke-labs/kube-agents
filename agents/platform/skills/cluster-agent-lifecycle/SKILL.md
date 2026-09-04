---
name: cluster-agent-lifecycle
description: Create, delegate to, and tear down per-cluster Cluster Agent Hermes profiles. Use whenever a GKE cluster is onboarded or deleted, or whenever a single-cluster runtime debugging/operations task should be delegated to that cluster's Cluster Agent.
---

# Cluster Agent Lifecycle Skill

As the Platform Agent you own the lifecycle of **Cluster Agents**. A Cluster Agent is a Hermes _profile_ — an isolated agent instance with its own persona (`SOUL.md`), scoped toolset, and home directory — that you create dynamically **inside your own pod**, one per managed GKE cluster. It handles read-only runtime operations and deep workload diagnostics on that single cluster, and returns its findings to you.

You never debug tenant workloads directly. You delegate that to the cluster's Cluster Agent and act on what it returns.

The engine for all of this is the helper script `scripts/cluster_agent_profile.py` (resolved at `/opt/data/scripts/cluster_agent_profile.py` at runtime).

## When to create a profile

Create the Cluster Agent profile as part of **cluster onboarding** — immediately after a cluster is successfully provisioned (see `gke-cluster-creation`) or when an existing cluster is first brought under management (see `manage-cluster`). A managed cluster and its Cluster Agent profile are created together: when you onboard or tear down a cluster, keep the profile in step with it. That rule is scoped to onboarding and teardown, where the profile is yours to manage. It is not a roster invariant to enforce from other work — the reconcile job below owns the roster, and an empty roster is a supported state, not damage to repair. In particular, the first-run discovery sweep must never create or repair profiles.

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

   The dispatcher spawns the Cluster Agent (`hermes -p <profile> chat -q "work kanban task <id>"`) automatically; it reads the card, does read-only diagnostics, and calls `kanban_complete(result=<the RCA>, summary=<one-line status>, metadata={...})`.

3. **Read the result** — you are auto-subscribed, so the completion (or a `needs_input` block) is pushed into your chat. You can also inspect it: `kanban_show(<id>)`. The RCA is in the card's `result` — the field the gateway posts verbatim, and the only one the requester receives — with any proposed patch in `metadata`; neither is ever in the worker's chat reply, which is a bare acknowledgement by design.

**Multi-cluster (fan-out):** create one card per cluster **with no `parents`**, in one burst, so they run in parallel. `parents` means "runs after", so a per-cluster card that lists your own running card as a parent can never be claimed — see `SOUL.md` §0. Then **keep your own card open**: poll each per-cluster card with `kanban_show(<id>)` (`sleep 60` between rounds), and once all of them are settled, synthesize their `result`/`metadata` into your own `kanban_complete(result=...)`. Completing your card is the delivery, so never complete it on a dispatch receipt — the image refuses a `kanban_complete` while your fanned-out cards are unfinished (#1010). See the **`workload-rebalancing`** skill for the validation-then-declare pattern.

## Acting on the result

The Cluster Agent is **read-only** and does not open Pull Requests. After reading the completed card:

1. Review the RCA in the card's `result` and the proposed manifest patch in its `metadata`.
2. If a change is warranted, **you** open (or update) the Pull Request via the `submit-suggestion` skill — you own the GitOps write path. Reconcile against any existing branch/PR for the same workload before creating a new one.
3. Report the outcome to the user as a clean SRE status update.

## When to delete a profile

Delete the Cluster Agent profile as part of **cluster teardown** (see `gke-cluster-creation`), after the cluster itself is removed:

```bash
python3 /opt/data/scripts/cluster_agent_profile.py delete \
  --project "<project>" --cluster "<cluster>" --location "<location>"
```

This deregisters the profile and removes its home directory. Do not delete a profile while its cluster still exists.

## Automatic reconciliation (create and prune)

The roster is also reconciled automatically. An hourly, deterministic `no_agent` cron job
(`cluster-agent-reconcile`) runs `scripts/cluster_agent_reconcile.py`, which drives the roster in
both directions:

- **Create** — every cluster in the project gets a profile, including the management cluster
  kube-agents itself runs on. The only exceptions are names listed in `RECONCILE_EXCLUDE`. If the
  pod cannot resolve the project, the create direction is skipped for that run rather than guessed
  at. The management cluster is included because its own workloads fail like any other cluster's,
  and the agent that triages a Kubernetes event is the one scoped to the cluster that raised it.
- **Prune** — a profile is deleted when its GKE cluster is definitively gone (a `NotFound` from
  `gcloud container clusters describe`), or when it belongs to an excluded cluster, which must not
  carry a profile even though that cluster exists. This closes the loop when a cluster is deleted
  out-of-band, so its profile is never left orphaned pointing at a dead kubeconfig.

A create that fails is recorded rather than only logged: the cluster goes into a `create_failed`
bucket. Both places that bucket surfaces are narrower than it looks. The chat summary names the
failures only on a run that also created or pruned something — a run whose sole outcome is a failed
create posts nothing, because a permanent cause repeats every minute while the bootstrap gate is
ticking. A failed create is only one of the things `--require-create-pass` reports. Under that flag the run
exits `3` when the CREATE direction never ran (the project would not resolve, or listing the
clusters failed), when `reconcile()` raised, or when it ran and every create failed with no fully
scaffolded profile already on the roster — and `4` when another reconcile holds the lock. One
failure alongside a success exits 0 and costs the sweep one `gaps` row. Without the flag every one
of those exits 0, because a cron producer must. That flag's caller is the bootstrap scan gate,
which gates a one-shot fleet sweep on the exit code, so a non-zero exit means "the roster is not
worth fanning out against yet", not specifically "a create failed".

A profile whose `cluster_identity` is stamped but whose `kubeconfig.yaml` or `USER.md` is missing is
treated as absent and recreated. Profile creation stamps the identity before it writes either file,
so a run killed mid-create leaves a home that satisfies the create direction's existence check and
the prune direction's "identity present, cluster exists" check — unrepaired, it stays half-built
forever.

It never deletes on ambiguity: any inconclusive check (auth/network/timeout, or a missing
`cluster_identity`) leaves the profile untouched. `created=0 pruned=0 kept=0` is a normal,
successful result. When it creates or prunes anything it posts a summary to every chat platform it
can resolve as enabled, falling back to Google Chat when it can resolve none.

Profile lifecycle belongs to this script and to the explicit onboarding/teardown paths above. Do
not repair the roster from other work by calling `cluster_agent_profile.py` directly — a profile
created for an excluded cluster is one the next run will prune.

To preview what would change without touching anything:

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
