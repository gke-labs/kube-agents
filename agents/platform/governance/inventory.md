# First-Time Environment Discovery & Inventory Scan (`bootstrap-inventory-scan`)

**Purpose:** Executes the background GKE environment discovery, topology inspection, and SRE workload audit on initial agent boot, generating the unified `/opt/data/INVENTORY.raw.md` file.

That file is the **complete** findings set, and it is not what the user receives. A separate
prioritization stage (`inventory_prioritize_sop.md`) ranks it down to the short report delivered to
chat as `/opt/data/INVENTORY.md`. Your job is to be thorough; being brief is the next stage's job.

---

## Pre-Execution Check

0. **Which card are you?** If your card body told you to resume this SOP at Step 4, you are the
   aggregation worker: go straight there and skip the status check below. It describes the state
   you are in — no `INVENTORY.raw.md`, no `INVENTORY.md` — and would send you back through
   discovery and the fan-out you were created to collect, re-filing your own card as its own
   parent and finishing onboarding with no report written.
1. **Verify Status:** Check directly via terminal command (`test -e /opt/data/INVENTORY.raw.md`) or directly inspect exact absolute file paths using `read_file` on `/opt/data/INVENTORY.raw.md`. **Do not run relative directory search patterns (`search_files`) since your active working directory (`cwd`) resides inside a subfolder where `/opt/data/` markers won't be listed.**
   - If `/opt/data/INVENTORY.md` is already built on disk, the whole flow has run: return strictly `[SILENT]` immediately and do nothing.
   - If `/opt/data/INVENTORY.raw.md` exists but `/opt/data/INVENTORY.md` does not, the sweep already finished and the handoff is what did not: **skip discovery entirely and go straight to Step 5** to file the prioritization card. Do not re-scan the fleet, and do not write the report yourself.
   - If both are confirmed absent, proceed through the systematic technical discovery process below.

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
you (`SOUL.md` §6). Create one card per cluster from Step 1 that has a Cluster Agent on the
roster, **all of them up front, in one
burst and with no `parents`**, so the dispatcher runs them concurrently:

```
kanban_create(
  assignee='<that cluster's Cluster Agent profile>',
  idempotency_key='bootstrap-inventory-cluster-<cluster>-<location>',
  title='Report cluster inventory: <cluster>',
  body=<the instructions below>,
)
```

The body must send that agent to the single-cluster SOP, reading whichever of these exists:

- `/opt/data/profiles/platform/governance/cluster_inventory_audit_sop.md`
- `/opt/platform-template/governance/cluster_inventory_audit_sop.md`

and tell it to complete its card with the structured `metadata` that SOP specifies.

**Point at the SOP; do not summarise it in the card body.** The checks are specific — probes,
requests and limits and the resulting QoS class, HPA coverage, `privileged` / `hostPID` /
`hostNetwork`, ResourceQuotas, LimitRanges, NetworkPolicies, Workload Identity — and so is the
`metadata` shape the aggregation stage reads. A body written freehand loses both, and what comes
back is a topology listing with no findings in it. That has been observed: four cards completed in
under two minutes each, every one of them with no `metadata` at all, and the fleet report that
followed named zero problems on a fleet that had them.

**Do not create, repair, or delete a Cluster Agent profile.** Profile lifecycle belongs to
`cluster_agent_reconcile.py`, which holds the `RECONCILE_EXCLUDE` opt-out and the create/prune
rules; a profile you create by hand is one the next reconcile run may immediately prune, and you
will loop. A cluster the roster does not cover is yours to audit in Step 4 — or, if you cannot
reach it, a row in the report saying so.

---

## Step 3: File the aggregation card and stand down

Create one more card, assigned to **yourself**, listing every per-cluster card from Step 2 in
`parents`. That is the fan-in: the dispatcher spawns you on it once all of them are done, with each
one's `metadata` in your context.

```
kanban_create(
  assignee='platform',
  idempotency_key='bootstrap-inventory-aggregate',
  title='Aggregate cluster inventory reports',
  parents=[<every per-cluster card id from Step 2>],
  body=<tell yourself to resume this SOP at Step 4>,
)
```

Run `python3 /opt/data/scripts/kanban_notify_propagate.py --to <card_id>` for the fan-in card so the user gets one closing
summary, then **complete this card**. Steps 4 to 6 run on the aggregation card, not this one.

**Do not wait here.** Blocking this card on the per-cluster cards deadlocks the board (`SOUL.md`
§0), and completing this card before the fan-in exists loses the fleet report entirely — the
per-cluster results are then metadata on cards nobody reads.

---

## Step 4: Compile Raw Inventory (`/opt/data/INVENTORY.raw.md`)

**This step and the two after it run on the aggregation card from Step 3.** Your input is the
`metadata` of every per-cluster card in your context — their `topology`, `workloads`,
`namespace_governance`, `findings` and `gaps`.

**Get the fleet list before you write anything.** If you came here as the aggregation card you did
not run Step 1 — a different card did, and none of its output reaches you — so run it now:

```
gcloud config get-value project
gcloud container clusters list --project=<that project>
```

If instead you are the Step 2 worker continuing here because no cluster had an agent, you already
have that list and must not re-run the enumeration. Either way the list, not the child cards, is
what makes the report whole: the children tell you only about clusters that had an agent to report,
the `Status` column has no other source, and any listed cluster that returned no `metadata` is one
nobody has audited.

**A cluster with no Cluster Agent has no `metadata`, and you audit it here yourself.** That is the
whole install when the roster is empty, and usually none of them otherwise — the reconcile gives
every listed cluster a profile — but derive the set by comparing the list against the clusters that
reported rather than assuming it is empty. Follow Steps 2 to 4 of `cluster_inventory_audit_sop.md`
for each, and record what you find in that SOP's Step 5 `metadata` shape: Step 2 is the
control-plane topology the fleet table's columns need, and Steps 3 and 4 are the probes,
requests/limits and QoS, HPA, security context, namespace governance, addons, observability and
hardening checks the Cluster Agents ran. Do not re-audit a cluster that did report; a cluster that
returned a `gaps` entry is a cluster whose gap you record.

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

One check is yours rather than theirs, because it reads a resource only this cluster has: before
you record an observability gap, read `.status.telemetry` on the PlatformAgent to see which
collector the agents are actually exporting to. A Cluster Agent pinned to a workload cluster cannot
see it, so it reports what it found on its own cluster and you reconcile.

Write the unified file `/opt/data/INVENTORY.raw.md`. **This is the complete findings set, and it is the only record of what the sweep saw — the prioritization stage reads this file and nothing else, so anything you omit here is invisible for the rest of onboarding.** Write in clean Markdown. Do not leave placeholders, "TODO", or truncated tables; fill in every value you discovered (use `n/a` only when a value genuinely does not apply).

Length is not a concern here and completeness is. This file is not delivered to chat directly; it is ranked down first, and it stays on disk so the user can ask for the full inventory later.

Structure the file in this order:

1. **Greeting Header:** A short, friendly heading and one or two sentences framing the report — e.g. a title like `# GKE Environment Discovery Report`, and a line noting this is the first-time environment scan for the project.

2. **GKE Fleet Discovery Table:** One row per discovered cluster.

   | Cluster Name | GCP Region / Zone | Status | K8s Version | Node Pools / Machine Types | Workload Identity | Observability Stack | Deployment Toolchain |
   | :----------- | :---------------- | :----- | :---------- | :------------------------- | :---------------- | :------------------ | :------------------- |

3. **Workloads Inventory Table:** One row per workload discovered across clusters.

   | Cluster | Namespace | Workload Name | Kind | Replicas (`Ready/Total`) | Probes (`Live/Ready`) | Resource QoS (`Req/Lim`) | OTel / Telemetry | Security Context (`NonRoot`) |
   | :------ | :-------- | :------------ | :--- | :----------------------- | :-------------------- | :----------------------- | :--------------- | :--------------------------- |

4. **Prioritized SRE Remediation Plan:** The full set of high-impact recommendations, grouped by priority — not just headings, but a concrete, actionable list under each:
   - **Priority 1 — Security & Identity Hardening** (Workload Identity, Shielded Nodes, Dataplane V2, Pod Security Admission, non-root/read-only filesystems).
   - **Priority 2 — Workload Reliability & Probes** (missing liveness/readiness/startup probes, resource requests/limits and QoS, HPA coverage).
   - **Priority 3 — Observability & Telemetry** (OpenTelemetry collection, Managed Service for Prometheus, SLO/error-budget alerting, missing standard SRE alerts).

   For each item, name the affected cluster/namespace/workload where applicable and state the recommended action concisely, so the reader can act on it directly.

   **Every `findings[]` entry from every per-cluster card belongs in one of these three groups.**
   This section is the only part of the file the prioritization stage can rank, so a finding a
   Cluster Agent reported and this list omits is a finding the user never sees. Carry its
   `severity` through — the next stage classifies against its own anchors, but it reads yours as
   the evidence for doing so.

---

## Step 5: Hand Off to Prioritization

Once `/opt/data/INVENTORY.raw.md` is fully written and confirmed on disk, file exactly one card to
rank it into the delivered report:

```
kanban_create(
  assignee='platform',
  idempotency_key='bootstrap-inventory-prioritize',
  title='Prioritize the onboarding inventory report',
  body=<the instructions below>,
)
```

The body must tell that worker to follow the prioritization SOP, reading whichever of these exists:

- `/opt/data/profiles/platform/governance/inventory_prioritize_sop.md`
- `/opt/platform-template/governance/inventory_prioritize_sop.md`

and to read `/opt/data/INVENTORY.raw.md` as its only input, writing the ranked report to
`/opt/data/INVENTORY.md`.

**Use that exact idempotency key.** Onboarding must happen once; the key is what makes a retry or a
duplicate of this card re-attach to the prioritization already in flight instead of writing the
report twice. One caveat: the board answers a repeated key with the id of the existing card, even a
completed one. If the create returns a card that has already completed and `/opt/data/INVENTORY.md`
is still absent, that earlier card failed without producing a report — file one more card with a
suffixed key (`bootstrap-inventory-prioritize-retry-1`) instead of reusing a key the board has
already answered.

**Do not prioritize the findings yourself.** Ranking runs as its own card on purpose: it must see the
raw findings and nothing else. Doing it here would rank them against the whole transcript of your
sweep instead, which produces a different report depending on how the sweep happened to go.

---

## Step 6: Silent Exit

Once the prioritization card is filed, return strictly `[SILENT]` immediately without running any
further terminal commands. Delivery to chat is handled separately by the
`bootstrap-inventory-delivery` job, after prioritization writes `/opt/data/INVENTORY.md` — do not
attempt to send the report yourself.
