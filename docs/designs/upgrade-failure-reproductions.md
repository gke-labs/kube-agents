# Reproducing the upgrade failure catalogue: which script plants each entry, what it showed, and what the fleet holds

This is the index that joins the three documents about upgrade failures.
[`upgrade-failure-catalogue.md`](upgrade-failure-catalogue.md) says, for each failure, what breaks,
which signal shows it before and after the upgrade, where to look, how to mitigate, what this
repository reads today and what GKE's Recommender publishes.
[`bench/upgrade-scenarios/README.md`](../../bench/upgrade-scenarios/README.md) holds the harness
that planted each entry on a throwaway cluster and, for all but entry 15, upgraded it, with its
verdicts and its evidence. This document says, per entry, which scenario script is the reproduction,
what that run showed in one paragraph, and what the seeded fleet (`bench/tf/fleet/`) holds for the
entry today, so that a case author knows where a fixture exists and where one would have to be
built. It restates nothing the other two own: the recipe is the script, the measurement is the
README's item, the check and the fix are the catalogue's entry.

## For a reader who does not run Kubernetes

The catalogue is the list of ways an upgrade can break an application. The harness is the set of
scripts that built a disposable cluster for each one, broke it on purpose, and kept the evidence.
The seeded fleet is the small set of long-lived test clusters every automated test run can look at,
which can carry a hazard in its before-state; no test may upgrade or change it, though GKE upgrades it on its own schedule, so a before-state there has to survive a node rebuild. This page lines the three up:
for each failure, which script reproduces it, what happened when it ran, and whether the shared test
clusters already carry the hazard, could, or never can.

## The list

Each line gives the entry, its verdict from the harness, the script the verdict rests on, and what
the seeded fleet on `main` holds for it. The fleet can carry a before-state only, and only one that GKE's own node rebuilds do not erase.

1. [A PodDisruptionBudget forbids the eviction](#1-a-poddisruptionbudget-forbids-the-eviction):
   reproduced; `scenarios/01.sh`; `readiness-drain-blocked` on seeded-b.
2. [No spare capacity for the displaced pods](#2-no-spare-capacity-for-the-displaced-pods):
   reproduced; `scenarios/02.sh`; `readiness-surge-blocked` on seeded-b.
3. [Every replica in one zone or on one node](#3-every-replica-in-one-zone-or-on-one-node):
   reproduced; `scenarios/03.sh`; the three `zonal-skew-*` roles on seeded-d.
4. [Data on the node is gone](#4-data-on-the-node-is-gone): reproduced; `scenarios/04.sh`; no role
   on `main`.
5. [Maintenance window too short, or an exclusion ends
   mid-roll](#5-maintenance-window-too-short-or-an-exclusion-ends-mid-roll): partial;
   `scenarios/05.sh`; covering exclusion on `main`.
6. [A served API version is removed](#6-a-served-api-version-is-removed): reproduced;
   `scenarios/06.sh`; no standing role possible; `deprecated-api-caller` stands in with a different
   label.
7. [A fail-closed webhook whose backend is not
   up](#7-a-fail-closed-webhook-whose-backend-is-not-up): reproduced; `scenarios/07.sh`; `readiness-failclosed-webhook` on seeded-b.
8. [A default changes in the new minor](#8-a-default-changes-in-the-new-minor): reproduced;
   `scenarios/08.sh`; version-bound, not for the fleet.
9. [A feature is deprecated but still served](#9-a-feature-is-deprecated-but-still-served): no break
   (as expected); `scenarios/09.sh`; `deprecated-api-caller` on `main`.
10. [Add-on and client skew](#10-add-on-and-client-skew): partial; `scenarios/10b.sh`; no role on
    `main`.
11. [The control plane is unreachable for minutes on a zonal
    cluster](#11-the-control-plane-is-unreachable-for-minutes-on-a-zonal-cluster): not reproduced;
    `scenarios/11b.sh`; no role needed.
12. [A node label is removed](#12-a-node-label-is-removed): reproduced (GKE form);
    `scenarios/12.sh`; no role on `main`.
13. [The container runtime changes](#13-the-container-runtime-changes): reproduced;
    `scenarios/13b.sh`; no role on `main`.
14. [cgroup v2 under a runtime that cannot read
    it](#14-cgroup-v2-under-a-runtime-that-cannot-read-it): reproduced (GKE form);
    `scenarios/14c.sh`; no role on `main`.
15. [The OOM killer starts killing the whole
    container](#15-the-oom-killer-starts-killing-the-whole-container): symptom reproduced;
    `scenarios/15.sh`; no role on `main`.
16. [The network dataplane changes](#16-the-network-dataplane-changes): reproduced (GKE form);
    `scenarios/16.sh`; seeded-a holds the before-state, no named role.
17. [A node networking agent fails on the new
    image](#17-a-node-networking-agent-fails-on-the-new-image): partial; `scenarios/17.sh`; no role
    on `main`.
18. [GPU driver mismatch](#18-gpu-driver-mismatch): partial; `scenarios/18k.sh`; never the fleet.
19. [In-tree volumes lose their CSI path](#19-in-tree-volumes-lose-their-csi-path): reproduced (GKE
    form); `scenarios/19c.sh`; no role on `main`.
20. [Images on a retired registry](#20-images-on-a-retired-registry): reproduced;
    `scenarios/20d.sh`; no role on `main`.

## The entries

Each entry opens with the script and the verdict its run produced, in the harness README's words,
then says what the fleet holds and where the rest lives. `bash run.sh NN` runs a scenario in a
project you name; the README's "How the scenarios were run" says what a run does, and its
"Re-reading and cleaning up" what it leaves behind.

### 1. A PodDisruptionBudget forbids the eviction

Harness: `scenarios/01.sh`; `bash run.sh 01`. Reproduced: the budget refused GKE's own drain (HTTP
429 to the container-engine robot in the audit log), GKE force-killed the pod 61 minutes after the
pool upgrade began, and the operation read DONE while the replacement was still Pending.

Fleet: `readiness-drain-blocked` on seeded-b (`pinned-batch-runner`'s `maxUnavailable: 0` budget;
`readiness-pinned-workload` is the role of the one-replica Deployment it protects) carries it.
Seeded-a's `inference-server` budget reads `disruptionsAllowed` 0 at runtime as a side effect of its
pinned pool, which the readiness check's spec-shape rule does not count.

Reproduction: `bench/upgrade-scenarios/scenarios/01.sh`, evidence
`bench/upgrade-scenarios/evidence/01/budget.txt`, and item 1 of the harness README. Detection, where
to look, mitigation and what reads it today: [catalogue entry
1](upgrade-failure-catalogue.md#1-a-poddisruptionbudget-forbids-the-eviction).

### 2. No spare capacity for the displaced pods

Harness: `scenarios/02.sh` (the verdict is from the `upg-02b` run); `bash run.sh 02`. Reproduced: on
a one-node pool with maxSurge 0 the drained replica was Pending for about four minutes and came back
only on the rebuilt node.

Fleet: `readiness-surge-blocked` on seeded-b (`no-surge-pool`, `maxSurge` 0 with `maxUnavailable` 1,
and the workload pinned to it) carries it; seeded-a's `pinned-inference-pool` (autoscaler 1/1) is the
ceiling half of the before-signal.

Reproduction: `bench/upgrade-scenarios/scenarios/02.sh`, evidence
`bench/upgrade-scenarios/evidence/02/capacity-availability.txt`, and item 2 of the harness README.
Detection, where to look, mitigation and what reads it today: [catalogue entry
2](upgrade-failure-catalogue.md#2-no-spare-capacity-for-the-displaced-pods).

### 3. Every replica in one zone or on one node

Harness: `scenarios/03.sh` (`upg-03b`); `bash run.sh 03`. Reproduced: two replicas pinned to one
node stopped in the same second and nothing served for 18 of 44 samples, while a spread Deployment
on the same pool never dropped; the one-zone case was not planted.

Fleet: the three `zonal-skew-*` roles on seeded-d, the multi-zonal slot (`zone-pinned-api`,
`zone-bound-store`, `capacity-starved-worker`), carry it; the other three clusters are single-zone.

Reproduction: `bench/upgrade-scenarios/scenarios/03.sh`, evidence
`bench/upgrade-scenarios/evidence/03/placement.txt`, and item 3 of the harness README. Detection,
where to look, mitigation and what reads it today: [catalogue entry
3](upgrade-failure-catalogue.md#3-every-replica-in-one-zone-or-on-one-node).

### 4. Data on the node is gone

Harness: `scenarios/04.sh` (`upg-04b`); `bash run.sh 04`. Reproduced: an emptyDir stamp survived the
control-plane upgrade and was replaced after the node rebuild, and nothing reported the loss; only
emptyDir was tested, not hostPath or Local SSD.

Fleet: no role on `main`. An `emptyDir` stamp would not hold, since the fleet's own node rebuilds erase it, but the volume source itself (a workload whose queue is an `emptyDir`) does, and that is the shape the catalogue's coverage section designs.

Reproduction: `bench/upgrade-scenarios/scenarios/04.sh`, evidence
`bench/upgrade-scenarios/evidence/04/node-data.txt`, and item 4 of the harness README. Detection,
where to look, mitigation and what reads it today: [catalogue entry
4](upgrade-failure-catalogue.md#4-data-on-the-node-is-gone).

### 5. Maintenance window too short, or an exclusion ends mid-roll

Harness: `scenarios/05.sh`; `bash run.sh 05`. Partial: a manual minor upgrade finished while a
NO_MINOR_UPGRADES exclusion was active and outside the daily window, which is the exclusion half; a
roll that outlasts its window and an exclusion that ends mid-roll cannot be forced in a test.

Fleet: the covering exclusion is on `main` (`hold-the-minor-lag` on seeded-b, `NO_MINOR_UPGRADES`);
the window and mid-roll halves cannot be planted.

Reproduction: `bench/upgrade-scenarios/scenarios/05.sh`, evidence
`bench/upgrade-scenarios/evidence/05/window.txt`, and item 5 of the harness README. Detection, where
to look, mitigation and what reads it today: [catalogue entry
5](upgrade-failure-catalogue.md#5-maintenance-window-too-short-or-an-exclusion-ends-mid-roll).

### 6. A served API version is removed

Harness: `scenarios/06.sh` and the hold `06h`; `bash run.sh 06`. Reproduced: after the control plane
moved to 1.32 the flowcontrol v1beta3 caller got a 404 while the stored object stayed readable
through v1. A long-lived cluster in the same project ran the caller through six daily Recommender
refreshes and DEPRECATION_K8S_1_32_API never appeared, while the audit log carried every write.

Fleet: no standing fleet role is possible (the entry needs a 1.31 control plane, which leaves the
EXTENDED channel on 2026-10-22); `deprecated-api-caller` on seeded-a stands in with a different
label, writes stamped `k8s.io/deprecated=true` rather than `k8s.io/removed-release`.

Reproduction: `bench/upgrade-scenarios/scenarios/06.sh`, evidence
`bench/upgrade-scenarios/evidence/06/removed-api.txt`, and item 6 of the harness README. Detection,
where to look, mitigation and what reads it today: [catalogue entry
6](upgrade-failure-catalogue.md#6-a-served-api-version-is-removed).

### 7. A fail-closed webhook whose backend is not up

Harness: `scenarios/07.sh`; `bash run.sh 07`. Reproduced: a fail-closed webhook with no endpoints
rejected the drain's replacement pods (guarded 0/2), and deleting the webhook brought both back
within a minute, which isolates it as the cause.

Fleet: `readiness-failclosed-webhook` on seeded-b (`seeded-fail-closed-gate`, `failurePolicy: Fail`
with the API's maximum timeout, pointing at a Service that does not exist) carries it.

Reproduction: `bench/upgrade-scenarios/scenarios/07.sh`, evidence
`bench/upgrade-scenarios/evidence/07/webhook.txt`, and item 7 of the harness README. Detection,
where to look, mitigation and what reads it today: [catalogue entry
7](upgrade-failure-catalogue.md#7-a-fail-closed-webhook-whose-backend-is-not-up).

### 8. A default changes in the new minor

Harness: `scenarios/08.sh` and the hold `08h`; `bash run.sh 08`. Reproduced: the 1.33 kubelet
refused the gitRepo volume with FailedMount on the new node.

Fleet: version-bound and not for the fleet: the before-state needs a kubelet below 1.33, which only
the EXTENDED channel offers.

Reproduction: `bench/upgrade-scenarios/scenarios/08.sh`, evidence
`bench/upgrade-scenarios/evidence/08/default-change.txt`, and item 8 of the harness README.
Detection, where to look, mitigation and what reads it today: [catalogue entry
8](upgrade-failure-catalogue.md#8-a-default-changes-in-the-new-minor).

### 9. A feature is deprecated but still served

Harness: `scenarios/09.sh`; `bash run.sh 09`. No break, as expected: Endpoints v1 writes carried
k8s.io/deprecated=true on 1.34 and still returned 200 after the move to 1.35; every writer Job
completed.

Fleet: `deprecated-api-caller` on seeded-a (`seeded-deprecation`) is this fixture.

Reproduction: `bench/upgrade-scenarios/scenarios/09.sh`, evidence
`bench/upgrade-scenarios/evidence/09/final.txt`, and item 9 of the harness README. Detection, where
to look, mitigation and what reads it today: [catalogue entry
9](upgrade-failure-catalogue.md#9-a-feature-is-deprecated-but-still-served).

### 10. Add-on and client skew

Harness: `scenarios/10.sh`, then `10b.sh` on the same cluster (`CLUSTER=upg-10 bash run.sh 10b`).
Partial: GKE let a 1.35 control plane run over 1.31 nodes, four minors apart, without refusing, and
a new pod, its logs and exec kept working; the add-on half was not planted and kubectl's skew
warning was not captured.

Fleet: no role on `main`; seeded-b's pool takes its control plane's pin on purpose, so the skew
cannot be planted there.

Reproduction: `bench/upgrade-scenarios/scenarios/10b.sh` (after `10.sh` on the same cluster),
evidence `bench/upgrade-scenarios/evidence/10b/skew.txt`, and item 10 of the harness README.
Detection, where to look, mitigation and what reads it today: [catalogue entry
10](upgrade-failure-catalogue.md#10-add-on-and-client-skew).

### 11. The control plane is unreachable for minutes on a zonal cluster

Harness: `scenarios/11.sh` (reads only) and `11b.sh` (a read and a write every two to four seconds);
`bash run.sh 11b`. Not reproduced: 228 samples through a zonal control-plane upgrade, all up; no gap
was measurable.

Fleet: no role needed; every slot is zonal, so the before-state holds everywhere.

Reproduction: `bench/upgrade-scenarios/scenarios/11b.sh`, evidence
`bench/upgrade-scenarios/evidence/11b/zonal.txt`, and item 11 of the harness README. Detection,
where to look, mitigation and what reads it today: [catalogue entry
11](upgrade-failure-catalogue.md#11-the-control-plane-is-unreachable-for-minutes-on-a-zonal-cluster).

### 12. A node label is removed

Harness: `scenarios/12.sh` (`upg-12b`); `bash run.sh 12`. Reproduced in GKE's form: a pod selecting
a label set by hand stayed Pending after the rebuilt node came back without it, and GKE reported
DONE; whether a minor still drops a standard label was not checked.

Fleet: no role on `main`. A hand-set node label would not hold through the node rebuild GKE's own patch upgrades perform, but a selector on a built-in deprecated label the kubelet still sets (`beta.kubernetes.io/arch`) does, and that is the designed shape.

Reproduction: `bench/upgrade-scenarios/scenarios/12.sh`, evidence
`bench/upgrade-scenarios/evidence/12/label.txt`, and item 12 of the harness README. Detection, where
to look, mitigation and what reads it today: [catalogue entry
12](upgrade-failure-catalogue.md#12-a-node-label-is-removed).

### 13. The container runtime changes

Harness: `scenarios/13.sh` and `13b.sh`; `bash run.sh 13b`. Reproduced: a patch-only node upgrade
inside 1.31 moved containerd from 1.7.34 to 2.0.10 and a v1alpha2 CRI client broke; run 13 found the
newest 1.31 patch already on containerd 2.0, so the runtime moves with a patch, not a minor.

Fleet: no role on `main`. The REGULAR clusters already run containerd 2, so a runtime-version fixture
could hold only the wreckage, but an agent mounting the containerd socket holds on any runtime, and
that is the designed shape.

Reproduction: `bench/upgrade-scenarios/scenarios/13b.sh`, evidence
`bench/upgrade-scenarios/evidence/13b/runtime.txt`, and item 13 of the harness README. Detection,
where to look, mitigation and what reads it today: [catalogue entry
13](upgrade-failure-catalogue.md#13-the-container-runtime-changes).

### 14. cgroup v2 under a runtime that cannot read it

Harness: `scenarios/14.sh` (the symptom on a v2 node, a legacy and a fixed JVM side by side) and
`14c.sh` (the full path; `CLUSTER=upg-14b bash run.sh 14c`). Reproduced in GKE's form: a cgroup v1
pool can still be created on a 1.34 cluster, GKE refused its 1.35 upgrade with a 400 and required
the migration to v2 first, and after the migration the old JVM was OOMKilled five times while a
fixed JVM stayed up.

Fleet: no role on `main`, and none would hold for long: GKE migrates a cgroup v1 pool to v2 at 1.33 and refuses v1 at 1.35, so the before-state has a shelf life the fleet's auto-upgrade sets.

Reproduction: `bench/upgrade-scenarios/scenarios/14c.sh` (`14.sh` is the symptom alone), evidence
`bench/upgrade-scenarios/evidence/14c/cgroup.txt`, and item 14 of the harness README. Detection,
where to look, mitigation and what reads it today: [catalogue entry
14](upgrade-failure-catalogue.md#14-cgroup-v2-under-a-runtime-that-cannot-read-it).

### 15. The OOM killer starts killing the whole container

Harness: `scenarios/15.sh` (`upg-15b`); `bash run.sh 15`. Symptom reproduced: on cgroup v2 a
three-process container over its limit was killed whole and crash-looped, and the same pod on a pool
with singleProcessOomKill true kept running; no upgrade crossed the 1.28 boundary, so only the
symptom is shown.

Fleet: no role on `main`; the symptom needs a multi-process container over its limit, which no
standing fixture should run.

Reproduction: `bench/upgrade-scenarios/scenarios/15.sh`, evidence
`bench/upgrade-scenarios/evidence/15/group-oom.txt`, and item 15 of the harness README. Detection,
where to look, mitigation and what reads it today: [catalogue entry
15](upgrade-failure-catalogue.md#15-the-oom-killer-starts-killing-the-whole-container).

### 16. The network dataplane changes

Harness: `scenarios/16.sh` and the hold `16h`; `bash run.sh 16`. Reproduced in GKE's form: a
default-deny NetworkPolicy sat unenforced, switching enforcement on changed nothing, and the next
pool upgrade rebuilt the node with calico and cut traffic; the trigger is the enforcement switch
applied at the rebuild, not a version change.

Fleet: no named role, but seeded-a holds the before-state: default-deny NetworkPolicies in three of its five seeded namespaces (`seeded-reliability`, `seeded-debug`, `seeded-capacity`; `seeded-security` has none on purpose and `seeded-deprecation` an egress-only policy) with neither the network-policy add-on nor Dataplane V2 enforcing them, which the first Recommender read counted as the catch for this entry.

Reproduction: `bench/upgrade-scenarios/scenarios/16.sh`, evidence
`bench/upgrade-scenarios/evidence/16/dataplane.txt`, and item 16 of the harness README. Detection,
where to look, mitigation and what reads it today: [catalogue entry
16](upgrade-failure-catalogue.md#16-the-network-dataplane-changes).

### 17. A node networking agent fails on the new image

Harness: `scenarios/17.sh` (`upg-17b`); `bash run.sh 17`. Partial: a stand-in per-node agent ran
only where a hand-set label was, the rebuilt node lacked the label, the DaemonSet went to zero and
the client on the new node got Connection refused, with the operation DONE; GKE's own node agents
cannot be broken from outside.

Fleet: no role on `main`. A hand-set label the DaemonSet selects on is what a node rebuild drops, so that shape would turn into the after-state, but a host-network agent mounting the CNI configuration directory holds, and that is the designed shape.

Reproduction: `bench/upgrade-scenarios/scenarios/17.sh`, evidence
`bench/upgrade-scenarios/evidence/17/node-agent.txt`, and item 17 of the harness README. Detection,
where to look, mitigation and what reads it today: [catalogue entry
17](upgrade-failure-catalogue.md#17-a-node-networking-agent-fails-on-the-new-image).

### 18. GPU driver mismatch

Harness: `scenarios/18k.sh` and `18m.sh` (the 1.33 to 1.34 leg on a T4, the runs the verdict rests
on) and `18i.sh`, all sourcing `scenarios/18.sh`, with `bench/upgrade-scenarios/compat-probe.sh` for
the forward-compatibility round; GPU capacity, not quota, decides where any of them can run.
Partial: on 1.33 the default driver was R535 and a plain CUDA 13 image failed as too old; the 1.34
upgrade installed R580, which opened the GPU for both plain images and refused the
forward-compatibility libraries a planted pod forced (Error 803). The upgrade removed the
catalogue's condition rather than creating it, and two runs lost their only GPU node to a stockout
mid-upgrade while the operation read DONE.

Fleet: no role on `main`, and the fleet carries no accelerator, so nothing GPU-bound can run; a suspended job that requests a GPU and pins a CUDA version holds as a manifest, and that is the designed shape.

Reproduction: `bench/upgrade-scenarios/scenarios/18k.sh` (`18m.sh` repeats it in another zone; both
source `18.sh`; the forward-compatibility round is `bench/upgrade-scenarios/compat-probe.sh`),
evidence `bench/upgrade-scenarios/evidence/18m/compat.txt`, and item 18 of the harness README.
Detection, where to look, mitigation and what reads it today: [catalogue entry
18](upgrade-failure-catalogue.md#18-gpu-driver-mismatch).

### 19. In-tree volumes lose their CSI path

Harness: `scenarios/19c.sh`, which sources `19.sh`; `ZONE=us-central1-c bash run.sh 19c`. Reproduced
in GKE's form: with the PD CSI driver add-on off, a running pod kept its in-tree disk, the pool
upgrade drained it, and the replacement stayed Pending on PersistentVolume node affinity rather than
failing at attach; re-enabling the driver brought it back 22 minutes after it went down. The 1.22
crossing itself cannot be built.

Fleet: no role on `main`; an in-tree `gcePersistentDisk` volume on seeded-a would carry the
before-state, with the PD CSI driver left on.

Reproduction: `bench/upgrade-scenarios/scenarios/19c.sh` (sources `19.sh`), evidence
`bench/upgrade-scenarios/evidence/19c/csi.txt`, and item 19 of the harness README. Detection, where
to look, mitigation and what reads it today: [catalogue entry
19](upgrade-failure-catalogue.md#19-in-tree-volumes-lose-their-csi-path).

### 20. Images on a retired registry

Harness: `scenarios/20d.sh`, which sources `20.sh`; `ZONE=us-central1-c bash run.sh 20d`.
Reproduced: a tag deleted from a private Artifact Registry repository stood in for a retired
registry; the pod restarted from the node's image cache on the old node and went to ImagePullBackOff
with not found on the rebuilt one. No hostname was retired and the egress-allowlist variant was not
tested.

Fleet: no role on `main`; a Deployment referencing an image on a retired registry hostname would
carry the before-state on seeded-a.

Reproduction: `bench/upgrade-scenarios/scenarios/20d.sh` (sources `20.sh`), evidence
`bench/upgrade-scenarios/evidence/20d/registry.txt`, and item 20 of the harness README. Detection,
where to look, mitigation and what reads it today: [catalogue entry
20](upgrade-failure-catalogue.md#20-images-on-a-retired-registry).
