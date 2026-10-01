# Seeded-fleet fixtures for the anomaly-detection checks

**Status:** plan. Nothing here is built. It says what the seeded fleet must grow for the fleet
anomaly-detection checks to be developed test-first, and which of those checks the fleet cannot
serve at all.

## Why this document

The anomaly-detection checks are being built eval-first: a case that fails against `main`, then the
behaviour that makes it pass ([`.agents/rules/eval_driven_development.md`](../../.agents/rules/eval_driven_development.md)).
That loop needs a planted defect to assert on, and the seeded fleet is where planted defects live —
three standing clusters per eval project, defects written as Terraform, each addressed by role
through [`bench/tf/fleet/fixtures.json`](../../bench/tf/fleet/fixtures.json) and catalogued in
[`bench-fleet-catalog.md`](bench-fleet-catalog.md).

Most anomaly checks have no fixture today, and one of them — zonal skew, the motivating case —
**cannot be planted on the fleet as it is shaped**. This document is the gap list and the proposal.

## The blocker: every seeded cluster is single-zone

`bench/tf/fleet/main.tf` gives all three clusters `location = var.zone`. A cluster whose nodes are
in one zone has no zonal distribution, so it cannot carry a skew, a topology-spread violation, or a
zone-pinned volume dragging a StatefulSet — the three causes the check is required to tell apart.
No amount of in-cluster planting works around the cluster's own shape.

Two ways out, and the cheap one is enough:

| Option                  | Shape                                                        | Standing cost on top of the management fee | Verdict                                                                             |
| ----------------------- | ------------------------------------------------------------ | ------------------------------------------ | ----------------------------------------------------------------------------------- |
| Regional cluster        | Regional control plane, nodes in three zones                 | A three-zone node floor                    | Rejected — the fleet is always on, and nothing here needs a regional control plane  |
| **Multi-zonal cluster** | **Zonal control plane, `node_locations` spanning two zones** | **Two small nodes**                        | **Proposed** — real per-zone distribution for the smallest node count that gives it |

Both options pay the same per-cluster GKE management fee, which is why the table prices only what
differs. That fee is the larger part of the change and the next section says so.

A multi-zonal cluster is the minimum shape that makes "pods are not spread across zones" a true
statement about a real cluster rather than a mock.

## Proposal: one new slot, `d`

The fleet's rule is that a defect lives on exactly one cluster so a red scenario points at one
place (the section banner above the cluster definitions in
[`main.tf`](../../bench/tf/fleet/main.tf)). Skew needs a cluster shape, not a
namespace, so it needs its own slot rather than a change to `a`, `b` or `c` — each of which already
carries a shape-level fixture that a re-shape would disturb.

`seeded-d`: zonal control plane, `node_locations` across two zones, one `e2-small` per zone, same
read-only posture, `managed-by=kube-agents-seeded-fleet` **and `environment=seeded`**.

The environment label is not optional and is the part of this proposal that needs the most care.
[`hack/fleet-kubeconfigs.sh`](../../hack/fleet-kubeconfigs.sh) discovers slots by filtering on
`resourceLabels.environment=seeded AND resourceLabels.managed-by=kube-agents-seeded-fleet`, so a
cluster without it is never listed, never resolves to a slot, and its roles are never published —
which fails the fleet-presence check on every pool project without a warning naming a cause.

Carrying it makes `seeded-d` a fourth voter in the configuration-drift cohort, which
[`main.tf`](../../bench/tf/fleet/main.tf)'s locals block warns about. The arithmetic has to be
worked rather than avoided. The cohort is keyed on environment, and the severity ladder walks one
step below an agreement ratio of 0.90 and another below 0.80. The planted facet is
`authorized-networks`, base `critical`: at three clusters the ratio is 2/3, two steps, surviving as
`minor`; at four, with `seeded-d` configured like `seeded-a` and `seeded-b`, it is 3/4 — still two
steps, still `minor`. What breaks it is leaving the authorized-networks block off `seeded-d`: 2/4 is
below the baseline threshold, so there is no baseline, no finding, and `consistency-drift-outlier`
reds. So `seeded-d` must carry that block, and it adds one declared compliance background row.

## What each check needs, and where it can come from

Grouped by whether the seeded fleet can serve it at all.

### Tier 1 — plantable in a namespace on `seeded-a` (cheap, no new infrastructure)

| Check                                          | Fixture to plant                                                                                                                                                                                                                 | Role name                      |
| ---------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------ |
| Namespaces with no ResourceQuota or LimitRange | A namespace without either, beside one that has both — **and a quota and limit range added to the four existing namespaces first**, which today have neither, so the planted one is currently indistinguishable from all of them | `quota-less-namespace`         |
| Plaintext secrets in the environment           | A Deployment with a credential-shaped `env` value                                                                                                                                                                                | `plaintext-secret-workload`    |
| Images outside the registry allowlist          | Already present twice: `defects-a.tf` runs unqualified `busybox:1.36` pulls. Either adopt one of those as the fixture or make the existing two qualified first                                                                   | `unallowlisted-image-workload` |
| Restart-count growth                           | A workload that restarts on a slow, steady cadence rather than crash-looping                                                                                                                                                     | `restart-trend-workload`       |
| Pending pods nothing can schedule              | A pod requesting more than any node offers                                                                                                                                                                                       | `unschedulable-pod`            |

Each is a Kubernetes object in `defects-a.tf`, a role in `fixtures.json` with its probes, and a
catalogue entry. No dollar cost beyond the objects themselves — but two of the five are not free in
effort, because the condition they plant is already true of `seeded-a` and has to be closed
elsewhere before the planted one means anything. Note also that `seeded-a`'s headroom is finite:
`main.tf` records that a single `e2-medium`'s allocatable CPU was fully claimed by system pods and
every fixture went Pending, which is why its pool is two nodes with roughly 600m to spare.

### Tier 2 — needs the new `seeded-d`

| Check                                          | Fixture                                                                                                                                                                                                                                                                  |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Zonal skew with cause: capacity                | A workload whose replicas request more than the second zone's node can offer, so some stay Pending on insufficient CPU. Two pools at different sizes would be the other way to get this; one balanced pool plus an oversized request is cheaper and needs no second pool |
| Zonal skew with cause: spread misconfiguration | A Deployment with `whenUnsatisfiable: ScheduleAnyway` and a node selector that only one zone satisfies                                                                                                                                                                   |
| Zonal skew with cause: zone-pinned volume      | A StatefulSet with a zonal PersistentVolumeClaim, which cannot move                                                                                                                                                                                                      |
| Blast radius                                   | Falls out of the three above — the share of replicas in the crowded zone is the measure                                                                                                                                                                                  |

One cluster, three roles, each a distinct cause so the check's attribution is what is under test
rather than its ability to notice imbalance.

### Tier 3 — project-level, no cluster needed

The fleet already carries a pair of these (`orphan_pd`, two unattached disks, catalogued as
`orphan-disks`), so the pattern exists.

| Check                             | Fixture                                                    |
| --------------------------------- | ---------------------------------------------------------- |
| Certificates and keys near expiry | A service-account key old enough to trip the age rule      |
| Idle reserved addresses           | A reserved static IP attached to nothing                   |
| Snapshots past retention          | A disk snapshot older than the retention the check asserts |

These sit on no cluster and belong in the project-scoped overlay
([`fleet-fixtures.yaml`](fleet-fixtures.yaml)).

### Tier 4 — the seeded fleet cannot serve these

Stated so nobody plans a fixture that cannot exist:

- **Committed-use coverage, Spot-share drift, chargeback, observability spend, cross-region
  egress.** These are billing and usage facts about a real account over time. A standing eval
  project has no committed-use discount, no meaningful spend history, and no billing export the
  agent may read (the cost SOP forbids it outright). Test these against recorded fixtures at the
  unit level, not against the fleet.
- **Control-plane load (API-server latency, etcd object growth, webhook p99).** Producing a
  measurable control-plane signal means loading the control plane, which a shared read-only fleet
  cannot host.
- **Accelerator utilization.** A standing GPU or TPU node is the most expensive thing this fleet
  could hold, for a check that is a threshold comparison. Mock it.

### Day-N gates

The catalogue's age rules apply to the new fixtures too
([`bench-fleet-catalog.md`](bench-fleet-catalog.md), "Day 0, 1, 7, 30"). The Tier 1 workloads are
assertable on apply day. The expiry fixtures in Tier 3 are assertable only once their own age
window passes, and the key-age one has no obvious answer: the catalogue says plainly that backdating is impossible and
not to try, so this fixture either waits out its own window after a replant or is dropped. Each new role states its day-N gate in the catalogue, and a case that asserts before the
gate is a case that fails for the environment rather than the agent.

## Order of work

1. `seeded-d` in `bench/tf/fleet/main.tf`, with the two-zone node pool and nothing planted on it
   yet. Exempt from the eval-first rule as infrastructure, but not inert: it joins the drift cohort
   on apply, so it needs its authorized-networks block and its catalogue slot in the same change, or
   the runner warns that it matches no declared slot on every presubmit. The catalogue edits a
   fourth slot needs are `cluster_slots` in `fixtures.json`, the `slots:` list in
   [`fleet-fixtures.yaml`](fleet-fixtures.yaml), and the fixture counts in
   [`bench-fleet-catalog.md`](bench-fleet-catalog.md).
2. The three zonal-skew fixtures on `seeded-d`, their roles, probes and catalogue entries.
3. The first failing eval case: skew with a stockout cause, asserting the agent names the zone,
   the cause and the blast radius. Red against `main` before any behaviour changes.
4. Tier 1 fixtures and their cases, one at a time, in the same red-then-green order.
5. Tier 3 project fixtures last: they are the slowest to become assertable and the least
   informative about the checks' hard part, which is attribution.

## What this costs

A fourth GKE management fee plus two `e2-small` nodes, standing, per eval project. The fee is the
larger of the two: [`bench/tf/fleet/README.md`](../../bench/tf/fleet/README.md) puts three of them
at most of the fleet's monthly total, with six nodes and two orphan disks making up the rest. No
regional control plane and no accelerators.

"No change to the three existing clusters" holds for the Tier 2 work and not for all of Tier 1: two
of those five fixtures need a condition closed on `seeded-a` before the planted one is
distinguishable, and the Tier 3 fixtures are billable — a reserved address that is attached to
nothing is charged precisely because it is idle, and snapshot storage is charged by size.
