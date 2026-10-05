# First-Time Environment Discovery & Inventory Scan (`bootstrap-inventory-scan`)

**Purpose:** The fleet half of first-time GKE environment discovery: enumerate the fleet, audit any
cluster that has no Cluster Agent, and complete.

You do not fan out and you do not write the report. The onboarding gate (`bootstrap_scan_gate.py`
and `bootstrap_handoff.py`, scripts) files one audit card per Cluster Agent itself, and once those
cards settle it writes their structured `metadata`, with yours, into `/opt/data/INVENTORY.raw.md`
and files the card that ranks it into the report delivered to chat as `/opt/data/INVENTORY.md`. Your
job is the fleet list, the clusters no Cluster Agent covers, and the gaps.

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

## Step 2: Do not fan out; the gate has

Your card lists the Cluster Agents the gate filed an audit card for, one each, read from their
profiles when it filed this card. **Do not create cluster cards, and do not look the roster up
yourself** — your terminal runs in a sandbox without the profiles' configuration, so a roster you
list there does not match. If the card lists none, there are no Cluster Agents: audit every cluster
from Step 1 yourself in Step 3.

**Do not create, repair, or delete a Cluster Agent profile.** Profile lifecycle belongs to
`cluster_agent_reconcile.py`, which holds the scope and its exclusions and the create/prune
rules; a profile you create by hand is one the next reconcile run may immediately prune, and you
will loop. A cluster the list on your card does not cover is yours to audit in Step 3 — or, if you cannot
reach it, an entry in `gaps` saying so.

---

## Step 3: Audit the clusters the list does not cover

**A cluster with no Cluster Agent has no card, and you audit it here yourself.** Those are the
clusters Step 1 listed that the list on your card does not name: all of them when the card
lists none, and usually none otherwise, because the reconcile gives every listed cluster a profile.
Take the set from the card's list, not from a roster you look up. Follow Steps 2 to 4 of
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
