# First-Time Environment Discovery & Inventory Scan (`bootstrap-inventory-scan`)

**Purpose:** Starts the first-time GKE environment discovery on initial agent boot: enumerate the
fleet, hand one audit card to each cluster's Cluster Agent, audit any cluster that has none, and
complete.

You do not write the report. Once the per-cluster cards settle, the onboarding gate
(`bootstrap_handoff.py`, a script) writes their structured `metadata` into
`/opt/data/INVENTORY.raw.md` and files the card that ranks it into the report delivered to chat as
`/opt/data/INVENTORY.md`. Your job is to make sure every cluster is either handed to a Cluster Agent,
audited by you, or named as a gap.

---

## Pre-Execution Check

1. **Verify Status:** Check `test -e /opt/data/INVENTORY.raw.md` with an absolute path. **Do not run
   relative directory search patterns (`search_files`): your working directory is a subfolder where
   `/opt/data/` files are not listed.**
   - If it exists, discovery already ran and was handed off: complete this card with a one-line
     `result` saying so, and do nothing else.
   - Otherwise proceed.

---

## Step 1: Environment Landscape & Fleet Discovery

Use native Google Cloud CLI (`gcloud`) and Kubernetes (`kubectl`) read-only commands to systematically map the project landscape:

1. **Identify GCP Project & Fleet Bounds:**
   - Run `gcloud config get-value project` and `gcloud container clusters list --project=<project-id>` to enumerate every active and stopped GKE cluster in the project.
2. **Inspect Cluster Control Planes & Topologies:**
   - For every running GKE cluster discovered (`e.g., kage-mgmt, platform-agent-host`), inspect its configuration: Kubernetes version, control plane region/zone, node pools (`machine types, node counts, autoscaling boundaries`), network configuration (`VPC-native, Dataplane V2 / eBPF`), and enabled GKE features (`Workload Identity, Managed Prometheus, OpenTelemetry collection`).
3. **Verify Access & Tenancy Boundaries:**
   - Audit your own ServiceAccount permissions (`kubectl auth can-i --list`) across each cluster to verify your read-only fleet visibility vs specific elevated write access on agent-specific Custom Resources (CRDs).

---

## Step 2: Fan the per-cluster audit out to the Cluster Agents

The workload audit is single-cluster runtime work, so each cluster's own Cluster Agent runs it, not
you (`SOUL.md` §6). **Your card lists one `kanban_create` call per Cluster Agent: make exactly
those calls and no others.** The gate read them from the Cluster Agent profiles when it filed this
card and kept only the ones ready to take a card, so a roster you look up yourself does not match
it. If the card lists none, there are no
Cluster Agents: skip to Step 3 and audit every cluster from Step 1 yourself. Make the calls **all up
front, in one burst, each with this card's id in `parents`**. Each has this shape:

```
kanban_create(
  assignee='<the Cluster Agent profile>',
  idempotency_key='bootstrap-inventory-cluster-<the Cluster Agent profile>',
  title='Report cluster inventory: `<cluster>` (`<project>`, `<location>`)',
  parents=[<this card's id>],
  body=<the instructions below>,
)
```

The body must send that agent to the single-cluster SOP, reading whichever of these exists:

- `/opt/data/profiles/platform/governance/cluster_inventory_audit_sop.md`
- `/opt/platform-template/governance/cluster_inventory_audit_sop.md`

and tell it to complete its card with the structured `metadata` that SOP specifies.

`parents` holds the per-cluster cards until this card completes. That is what lets you complete it
in Step 4 straight away: the board refuses a completion while cards this card filed are unfinished,
unless this card is their parent. They start as soon as you complete.

**Point at the SOP; do not summarise it in the card body.** The checks are specific — probes,
requests and limits and the resulting QoS class, HPA coverage, `privileged` / `hostPID` /
`hostNetwork`, ResourceQuotas, LimitRanges, NetworkPolicies, Workload Identity — and so is the
`metadata` shape the hand-off reads. A body written freehand loses both, and what comes
back is a topology listing with no findings in it. That has been observed: four cards completed in
under two minutes each, every one of them with no `metadata` at all, and the fleet report that
followed named zero problems on a fleet that had them.

**Do not create, repair, or delete a Cluster Agent profile.** Profile lifecycle belongs to
`cluster_agent_reconcile.py`, which holds the scope and its exclusions and the create/prune
rules; a profile you create by hand is one the next reconcile run may immediately prune, and you
will loop. A cluster no call on your card covers is yours to audit in Step 3 — or, if you cannot
reach it, an entry in `gaps` saying so.

---

## Step 3: Audit the clusters no call covers

**A cluster with no Cluster Agent has no card, and you audit it here yourself.** Those are the
clusters Step 1 listed that no `kanban_create` call on your card names: all of them when the card
lists none, and usually none otherwise, because the reconcile gives every listed cluster a profile.
Take the set from the card's calls, not from a roster you look up. Follow Steps 2 to 4 of
`cluster_inventory_audit_sop.md` for each, and record what you find in that SOP's Step 5 `metadata`
shape: Step 2 is the control-plane topology, and Steps 3 and 4 are the probes, requests/limits and
QoS, HPA, security context, namespace governance, addons, observability and hardening checks the
Cluster Agents run.

**Pin `kubectl` to each cluster before you run a single command against it.** That SOP is written
for a Cluster Agent whose `KUBECONFIG` already points at one cluster; yours does not. Bare
`kubectl` from this profile resolves to the credential proxy's own context — the management cluster
— so an audit run unpinned files the management cluster's workloads under someone else's name, and
nothing downstream catches it. Use the per-target recipe under **Cluster Credentials** in `AGENTS.md`
in your own profile home, and build the MCP `projects/…/clusters/…` parent from the row you got out
of `gcloud container clusters list` — that SOP says to take it from `USER.md`, which describes a
Cluster Agent's own cluster and not one you are auditing on its behalf. A cluster is very often
uncovered precisely because credentials for it could not be minted; if that happens to you too,
record it as unaudited and why, and audit nothing on it.

For the clusters you audit here, one check reads a resource only this cluster has: before you
record an observability gap, read `.status.telemetry` on the PlatformAgent to see which collector
the agents are actually exporting to. Report it as `telemetry` in Step 4 as well: the hand-off puts
it in the raw report beside the Cluster Agents' observability findings, which could not see it.

---

## Step 4: Complete the Card

**Complete this card now. Do not wait for the per-cluster cards, do not write
`/opt/data/INVENTORY.raw.md` or `/opt/data/INVENTORY.md`, and do not file a ranking card** — the
onboarding gate does all three once the per-cluster cards settle. Waiting is what this card used to
do, and it has no tool that waits reliably.

Call `kanban_complete` with a short factual `result` (clusters listed, cards filed, clusters you
audited) and this `metadata`:

```json
{
  "fleet": [{ "project": "…", "cluster": "…", "location": "…", "status": "RUNNING" }],
  "clusters": [<one Step 3 audit per cluster, in the single-cluster SOP's metadata shape>],
  "telemetry": "<the PlatformAgent's .status.telemetry, one line>",
  "gaps": ["<anything you could not do, and why>"]
}
```

`fleet` is every cluster Step 1 listed: the hand-off names any of them that no audit reported on, so
a silent gap does not read as a clean cluster. `clusters` is an empty list when every cluster had an
agent. `telemetry` is the collector the agents export to: read `.status.telemetry` on the PlatformAgent
once and summarise it. A Cluster Agent cannot see that resource, so the report shows it beside their
observability findings. A cluster you could not reach goes in `gaps`, not silently out of `fleet`. If you could not
list the clusters at all, say so in `gaps` and complete anyway — onboarding runs once.

Then return strictly `[SILENT]`. Delivery to chat is handled separately; do not send anything to the
user.
