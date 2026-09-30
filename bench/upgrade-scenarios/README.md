# Upgrade failure scenarios: what reproduced, and what GKE's Recommender caught

## In plain terms

When these runs were made, the upgrade failure catalogue listed twenty ways a GKE version upgrade can take an
application down. For each one we built a throwaway cluster, planted the problem, ran the upgrade, and
recorded what happened. For scenario 15, only the symptom was produced, without an upgrade. Each verdict was
then handed to a separate reviewer whose job was to prove it wrong.

Thirteen of the twenty failures happened for real. In four of those, the failure came about in a different way
than the catalogue describes, because GKE takes its own path. For example, GKE refuses one upgrade and demands a
migration, and the application breaks during the migration. One more produced the right symptom without the
upgrade being the cause. Four were only partly forced. In one of them, the GPU scenario, the upgrade did break
GPU programs, but the reverse way round: the catalogue expects the new GPU driver to be too old, and GKE
installed a newer one, which refused a compatibility workaround we had added and which the old driver accepted.
One did not happen at all: the control plane of a single-zone cluster stayed reachable throughout its own
upgrade. One was expected not to break, and did not.
The GPU scenario needed fourteen clusters across twelve zones, because GPUs were sold out in ten of them. In
two of those runs the shortage struck mid-upgrade: GKE deleted the only GPU node, could not create its
replacement, and still reported the upgrade as done.

GKE has a built-in upgrade advisor, the Recommender, which is supposed to warn you before an upgrade. GKE
documents a check for only six of the twenty problems, so it cannot warn about the other fourteen however long
it runs. So far it has flagged two of the twenty: the eviction budget that blocks draining (scenario 1), and
network policies that nothing enforces (scenario 16). Both were flagged on long-lived clusters. A third check,
for an API the next version removes (scenario 6), has had five daily refreshes on a cluster calling that API
and has not fired. The newest refresh is from before the scenario clusters existed, so the remaining three
documented checks (scenarios 7, 10 and 13) have not had their chance yet.

This directory holds the scripts that built and broke those clusters, the table of results, and the one
evidence file behind each row. Running them creates real GKE clusters in a project you name and upgrades them
until they break, so point them only at a project of your own.

The practical conclusion: an upgrade-readiness check cannot rely on the Recommender. It has to look for these
problems directly, and treat a Recommender insight as a bonus.

## The table

`results.py` generates everything between the markers, and [`results.csv`](results.csv) beside it, from the
verdicts it holds and from `recommender.json`. It writes nothing and exits 1 if a quote does not appear word
for word in its evidence file. Run prettier on `README.md` after it, at the version
`.github/workflows/prettier.yml` pins: the repository's prettier check aligns the table, and `results.py`
writes it unaligned. GitHub shows `results.csv` as a searchable table with one row per scenario and the full quote (tabs shown as
spaces). The proof column below shows at most 150 characters of each quote, with tabs shown as single spaces
(rows 1 and 5). `grep -F` on the text after the tab, or `grep -P` with `\t`, finds the full line and the command
that produced it.

<!-- BEGIN TABLE -->

Verdicts: reproduced 13, partial 4, no break 1, not reproduced 1, symptom reproduced 1. Recommender read at 2026-09-29T1747Z in 12 zones (newest refresh 2026-09-28T00:00:00Z). **Caught: 2 of 20.** GKE documents a check for 6 of the 20.

| #   | Failure                                                         | Reproduced?            | Proof (evidence file: quoted line)                                                                                                                                     | GKE Recommender check for it                                                     | Published on the clusters carrying it                                                                                                                                                                                   | Caught?                                |
| --- | --------------------------------------------------------------- | ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------- |
| 1   | A PodDisruptionBudget forbids the eviction                      | reproduced             | `01/budget.txt`: `2026-09-29T15:00:13.790784Z service-<PROJECT_NUMBER>@container-engine-robot.iam.gserviceaccount.com`                                                 | PDB_UNPERMISSIVE                                                                 | `upg-01`: no matching insight; `gemma-gpu`: PDB_UNPERMISSIVE (unrelated: 4); `gemma-gpu-upgraded`: PDB_UNPERMISSIVE (unrelated: 4)                                                                                      | caught (PDB_UNPERMISSIVE)              |
| 2   | No spare capacity for the displaced pods                        | reproduced             | `02/capacity-availability.txt`: `2026-09-29T14:45:24Z /Pending/`                                                                                                       | none documented                                                                  | `upg-02b`: no matching insight                                                                                                                                                                                          | no check exists                        |
| 3   | Every replica in one zone or on one node                        | reproduced             | `03/placement.txt`: `samples=44 zero_serving=18`                                                                                                                       | none documented                                                                  | `upg-03b`: no matching insight                                                                                                                                                                                          | no check exists                        |
| 4   | Data on the node is gone                                        | reproduced             | `04/node-data.txt`: `Tue Sep 29 15:17:26 UTC 2026`                                                                                                                     | none documented                                                                  | `upg-04b`: no matching insight                                                                                                                                                                                          | no check exists                        |
| 5   | Maintenance window too short, or an exclusion ends mid-roll     | partial                | `05/window.txt`: `1.35.8-gke.1380000 hold-minor={'endTime': '2026-10-02T13:49:59Z', 'maintenanceExclusionOptions': {'scope': 'NO_MINOR_UPGRADES'}`                     | none documented                                                                  | `upg-05`: no matching insight                                                                                                                                                                                           | no check exists                        |
| 6   | A served API version is removed                                 | reproduced             | `06/removed-api.txt`: `no longer serves flowcontrol.apiserver.k8s.io/v1beta3 (discovery returned 404)`                                                                 | DEPRECATION_K8S_1_32_API                                                         | `upg-06h`: no matching insight; `gemma-gpu`: no matching insight (unrelated: 5)                                                                                                                                         | not yet                                |
| 7   | A fail-closed webhook whose backend is not up                   | reproduced             | `07/webhook.txt`: `14:10:50Z   FailedCreate        guarded-7cf87d9df4         Error creating: Internal error occurred: failed calling webhook "gate.scen.example.com"` | K8S_ADMISSION_WEBHOOK_UNAVAILABLE                                                | `upg-07`: no matching insight                                                                                                                                                                                           | not yet                                |
| 8   | A default changes in the new minor                              | reproduced             | `08/default-change.txt`: `git-repo volume plugin has been disabled`                                                                                                    | none documented                                                                  | `upg-08h`: no matching insight                                                                                                                                                                                          | no check exists                        |
| 9   | A feature is deprecated but still served                        | no break (as expected) | `09/final.txt`: `2026-09-29T14:05:15Z   Completed          after                      Job completed`                                                                   | none documented                                                                  | `upg-09`: no matching insight                                                                                                                                                                                           | no check exists                        |
| 10  | Add-on and client skew                                          | partial                | `10b/skew.txt`: `gke-upg-10-work-pool-6fb8c002-0fdz      Ready    <none>   47m     v1.31.14-gke.2759000`                                                               | CLUSTER_VERSION_SKEW_UNSUPPORTED                                                 | `upg-10`: no matching insight                                                                                                                                                                                           | not yet                                |
| 11  | The control plane is unreachable for minutes on a zonal cluster | not reproduced         | `11b/zonal.txt`: `read_down=0`                                                                                                                                         | none documented                                                                  | `upg-11b`: no matching insight                                                                                                                                                                                          | no check exists                        |
| 12  | A node label is removed                                         | reproduced (GKE form)  | `12/label.txt`: `0/3 nodes are available: 1 node(s) were unschedulable, 2 node(s) didn't match Pod's node affinity/selector.`                                          | none documented                                                                  | `upg-12b`: no matching insight                                                                                                                                                                                          | no check exists                        |
| 13  | The container runtime changes                                   | reproduced             | `13b/runtime.txt`: `unknown service runtime.v1alpha2.RuntimeService`                                                                                                   | DEPRECATION_CONTAINERD_V1ALPHA2_CRI_API, DEPRECATION_CONTAINERD_V1_SCHEMA_IMAGES | `upg-13b`: no matching insight                                                                                                                                                                                          | not yet                                |
| 14  | cgroup v2 under a runtime that cannot read it                   | reproduced (GKE form)  | `14c/cgroup.txt`: `legacy-jvm-v1-6c5f68468-xq87r   gke-upg-14b-v1-pool-4922a2a8-cej9   Running   5          OOMKilled   137`                                           | none documented                                                                  | `upg-14b`: no matching insight                                                                                                                                                                                          | no check exists                        |
| 15  | The OOM killer starts killing the whole container               | symptom reproduced     | `15/group-oom.txt`: `forker-default   Running   4          OOMKilled`                                                                                                  | none documented                                                                  | `upg-15b`: no matching insight                                                                                                                                                                                          | no check exists                        |
| 16  | The network dataplane changes                                   | reproduced (GKE form)  | `16/dataplane.txt`: `wget: download timed out`                                                                                                                         | NETWORK_POLICIES_UNRECONCILED                                                    | `upg-16h`: no matching insight; `seeded-a`: NETWORK_POLICIES_UNRECONCILED (unrelated: 1); `gemma-gpu`: NETWORK_POLICIES_UNRECONCILED (unrelated: 4); `gemma-gpu-upgraded`: NETWORK_POLICIES_UNRECONCILED (unrelated: 4) | caught (NETWORK_POLICIES_UNRECONCILED) |
| 17  | A node networking agent fails on the new image                  | partial                | `17/node-agent.txt`: `wget: can't connect to remote host (10.128.0.59): Connection refused`                                                                            | none documented                                                                  | `upg-17b`: no matching insight                                                                                                                                                                                          | no check exists                        |
| 18  | GPU driver mismatch                                             | partial                | `18m/compat.txt`: `Error 803: system has unsupported display driver / cuda driver combination`                                                                         | none documented                                                                  | `upg-18m`: no matching insight; `upg-18k`: no matching insight; `upg-18i`: no matching insight                                                                                                                          | no check exists                        |
| 19  | In-tree volumes lose their CSI path                             | reproduced (GKE form)  | `19c/csi.txt`: `pd-user-5f66bd9f9b-v6kmc   0/3 nodes are available: 1 node(s) didn't match PersistentVolume's node affinity`                                           | none documented                                                                  | `upg-19c`: no matching insight                                                                                                                                                                                          | no check exists                        |
| 20  | Images on a retired registry                                    | reproduced             | `20d/registry.txt`: `retired-image-5b5c57c885-kzjq8   0/1     ImagePullBackOff`                                                                                        | none documented                                                                  | `upg-20d`: no matching insight                                                                                                                                                                                          | no check exists                        |

Every insight or recommendation published on a scenario cluster, including ones unrelated to its scenario:

| Cluster              | Subtypes                                                                                                                          | Last refresh         |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------- | -------------------- |
| `gemma-gpu`          | NETWORK_POLICIES_UNRECONCILED, NODE_SA_MISSING_PERMISSIONS, PDB_UNPERMISSIVE, WORKLOAD_OVERPROVISIONED, WORKLOAD_UNDERPROVISIONED | 2026-09-28T00:00:00Z |
| `gemma-gpu-upgraded` | NETWORK_POLICIES_UNRECONCILED, NODE_SA_MISSING_PERMISSIONS, PDB_UNPERMISSIVE, WORKLOAD_OVERPROVISIONED, WORKLOAD_UNDERPROVISIONED | 2026-09-28T00:00:00Z |
| `seeded-a`           | NETWORK_POLICIES_UNRECONCILED, NODE_SA_MISSING_PERMISSIONS                                                                        | 2026-09-28T00:00:00Z |
| `upg-01`             | none                                                                                                                              |                      |
| `upg-02b`            | none                                                                                                                              |                      |
| `upg-03b`            | none                                                                                                                              |                      |
| `upg-04b`            | none                                                                                                                              |                      |
| `upg-05`             | none                                                                                                                              |                      |
| `upg-06h`            | none                                                                                                                              |                      |
| `upg-07`             | none                                                                                                                              |                      |
| `upg-08h`            | none                                                                                                                              |                      |
| `upg-09`             | none                                                                                                                              |                      |
| `upg-10`             | none                                                                                                                              |                      |
| `upg-11b`            | none                                                                                                                              |                      |
| `upg-12b`            | none                                                                                                                              |                      |
| `upg-13b`            | none                                                                                                                              |                      |
| `upg-14b`            | none                                                                                                                              |                      |
| `upg-15b`            | none                                                                                                                              |                      |
| `upg-16h`            | none                                                                                                                              |                      |
| `upg-17b`            | none                                                                                                                              |                      |
| `upg-18i`            | none                                                                                                                              |                      |
| `upg-18k`            | none                                                                                                                              |                      |
| `upg-18m`            | none                                                                                                                              |                      |
| `upg-19c`            | none                                                                                                                              |                      |
| `upg-20d`            | none                                                                                                                              |                      |

<!-- END TABLE -->

A verdict of **reproduced** means the failure the catalogue describes happened, and the upgrade caused it.
**Reproduced (GKE form)** means the failure happened and the upgrade triggered it, but GKE's path to it differs from
the catalogue's wording. **Symptom reproduced** means the end symptom was produced, but not by an upgrade.
**Partial** means only part of the mechanism could be forced. **Partial** also covers 18, where the upgrade caused a related
failure only with a planted precondition. **Not reproduced** means the precondition was in place and the
failure did not happen, or the precondition could not be put in place. **No break (as expected)** means the
catalogue predicts nothing breaks yet, and nothing did.

## What each verdict rests on

Every line below survived the adversarial pass. Times are UTC on 2026-09-29. Only the evidence file each table
row quotes is checked in, under `evidence/`, with the project ID and project number replaced by placeholders.
The other files named below were recorded in the same runs and are not in the repository.

1. The budget refused GKE's own drain: the audit log shows `container-engine-robot` getting HTTP 429 on
   eviction. GKE then force-killed the pod at 15:00:16, 61 minutes after the pool upgrade began at 13:59:14. The
   operation read DONE at 15:02:11 while the replacement was still Pending. Only the last three 429s are on
   record, because the audit query was limited to three rows, so the hour-long refusal is inferred from the
   operation's timeline. The replacement stayed Pending for lack of CPU on an e2-small node, a confound that is
   separate from the budget.
2. The pool was one node with `maxSurge 0`. The drained replica was Pending for about four minutes (22 samples,
   14:45:24 to 14:49:25) and came back only on the rebuilt node. No surge node appeared.
3. Two replicas were pinned to one node; the one-zone case was not planted. GKE stopped both in the same second, and nothing served for 18 of 44
   samples. A spread Deployment on the same pool is the control, and it never dropped. The required
   `podAffinity` held the replacements together, which made the outage about 3.5 minutes. Plain co-location
   would probably be closer to one minute.
4. An `emptyDir` stamp survived the control-plane upgrade (14:54:54 both times) and was replaced by 15:17:26
   after the node rebuild. Nothing reported the loss. Only `emptyDir` was tested, not `hostPath` or local SSD.
5. Only the exclusion half was forced. A manual minor upgrade finished while a `NO_MINOR_UPGRADES` exclusion
   was active and outside the daily window. A roll that outlasts its window, and an exclusion that ends
   mid-roll, cannot be forced in a test.
6. After the control plane moved to 1.32, the `flowcontrol.apiserver.k8s.io/v1beta3` caller got a 404. The
   stored object stayed readable through `v1`.
7. A fail-closed webhook with no endpoints rejected the drain's replacement pods (`guarded 0/2`). Deleting the
   webhook at 14:13:51 brought both back: the ReplicaSet created them at 14:14:23, and both were Running at
   14:14:37. That isolates the webhook as the cause. One earlier replica loss was a
   small-node preemption, not the webhook.
8. The 1.33 kubelet refused the `gitRepo` volume with `FailedMount` on the new node.
9. Endpoints v1 writes carried `k8s.io/deprecated=true` on 1.34. After the move to 1.35 the writer still got
   HTTP 200, but the audit label was not re-read. Every writer Job completed. This is the "nothing breaks yet" case the catalogue predicts.
10. GKE let a 1.35 control plane run over 1.31 nodes, four minors apart. That is beyond GKE's own two-minor
    policy and upstream's three-minor maximum. GKE did not refuse it, and a new pod, its logs and exec all kept
    working against the 1.31 kubelets. A 1.29 kubectl worked against the earlier 1.34 control plane and was not
    re-run on 1.35. The add-on half (an add-on off its vendor matrix) was not
    planted, and kubectl's skew warning was not captured.
11. A probe read and wrote every two to four seconds, from 10 seconds before the zonal control-plane upgrade to
    30 seconds after it: 228 samples, about 211 inside the operation, all up. The file's `samples=229` also
    counts its header line. The read-only run on `upg-11` saw 225 up. No gap was measurable.
12. A pod selected a label set by hand with `kubectl`. The rebuilt node did not carry it, and the pod stayed
    Pending after GKE reported DONE. GKE form: the run tested a label the node pool does not declare, which
    a rebuild drops. Whether 1.35 still sets every standard label was not checked.
13. A v1alpha2 CRI client broke when a patch-only node upgrade inside 1.31 moved containerd from 1.7.34 to
    2.0.10. The schema-1 image half shows only on the held cluster (`13-hold`). There was no CrashLoop, because
    the probe prints its error and keeps running.
14. An old JVM (11.0.15) ran correctly on a cgroup v1 pool. GKE refused that pool's 1.35 upgrade with a 400
    and required migration to cgroup v2 first. After the migration, the same image was OOMKilled five times. A fixed JVM
    (11.0.16), left from run 14 on the cluster's always-v2 `work-pool`, stayed up with no restarts. GKE form: GKE does not flip a pinned v1 pool during the upgrade; it
    blocks the upgrade until you do.
15. On cgroup v2, a three-process container over its limit was killed whole and went into CrashLoopBackOff.
    The same pod on a pool with `singleProcessOomKill: true` kept running. No upgrade crossed the boundary, so
    only the symptom is shown.
16. A default-deny NetworkPolicy sat unenforced. Switching enforcement on changed nothing (connect still
    worked at 14:44:43), and the next pool upgrade rebuilt the node with calico and cut traffic. GKE form: the
    trigger is the enforcement switch applied at the rebuild, not a version change.
17. A per-node agent ran only where a hand-set label was. The rebuilt node lacked the label, so the DaemonSet
    went from one desired pod to zero, and the client on the new node got `Connection refused`. The operation
    read DONE with no warning. Only this half is forced: GKE's own node networking agents cannot be broken
    from outside.
18. `upg-18m` (`us-west1-a`) and `upg-18k` (`us-central1-b`), each on one time-shared T4 with `maxSurge 1`, gave
    the same result. On 1.33.13 the `default` driver setting installed 535.309.01. A plain CUDA 12.4 PyTorch
    image opened the GPU, and a plain CUDA 13.0 image did not (`The NVIDIA driver on your system is too old`).
    The probe script then planted a forward-compatibility setup that neither image uses on its own: an init
    container copied NVIDIA's compatibility libraries from the matching `nvidia/cuda` base image
    (`libcuda.so.550.54.15`, `libcuda.so.580.65.06`), and the probe put them first on `LD_LIBRARY_PATH`. The
    CUDA 12.4 image did not need them. Both planted pods opened the GPU on 1.33. The pool upgrade to 1.34.11
    (18m: operation 17:21:03 to 17:23:04) created a node whose installer logged `Installing GPU driver version
580.173.02`. On that node both planted pods failed with `Error 803: system has unsupported display driver /
cuda driver combination`, and both plain images opened the GPU, from the same tags at the same image sizes;
    the planted pods copied identical compatibility files both times. The upgrade caused the failure, and
    `Error 803` names the driver and CUDA combination, but no control isolates the driver from the kernel (6.6 to
    6.12) and containerd (2.0.10 to 2.2.7) changes that came with it. Partial: the catalogue's
    condition is a new driver older than the CUDA build needs, and the upgrade removed that condition rather
    than creating it (plain CUDA 13.0 failed on R535 in `18i` on 1.32 and 1.33 and in `18k` and `18m` on 1.33,
    and worked on R580). What broke was the reverse, a driver newer than the compatibility libraries a pod
    forces, which is NVIDIA's rule on any platform and needed the planted setup. The catalogue's "after"
    symptoms did not appear in `18k` or `18m`: every probe pod was scheduled, and the node's host-mounted
    `nvidia-smi` read the driver in every pod; only opening the GPU failed. The runs saw three versions
    (1.32.13 and 1.33.13 install 535.309.01, 1.34.11 installs 580.173.02) and do not show what other versions
    install, or that the `default` setting never lowers the driver. `upg-18i` (`us-east1-d`, `maxSurge 0`) ran
    the plain probes on 1.32 and 1.33 with the same results and the compatibility probe on 1.33 only (the pods
    tagged `v132` started after the node was rebuilt on 1.33). It then lost its only GPU node on the 1.34 step
    when Compute Engine could not recreate it (`ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS`), and its 1.34 probe
    pods never ran.
19. `upg-19c` in `us-central1-c`. A running pod on 1.34 had mounted an in-tree `gcePersistentDisk` volume. The PD
    CSI driver add-on was then turned off (the add-on setting read empty at 16:01:10), and the pod kept running.
    The 1.35 pool upgrade drained the node at 16:12:03, and the old pod stopped being ready between 16:12:32
    and 16:12:43. Its replacement stayed `Pending` with `didn't match PersistentVolume's node affinity`. No event
    names the node that turned it away. The new 1.35 node is the only one it can be, since the other two were
    ruled out by the pod's node selector and by the cordon. The pod was watched in that state for 12 minutes
    before the script switched the driver back on at 16:24:04. The switch finished at about 16:33:30 (inferred
    from the script's timing; no end time was recorded). The pod was scheduled at 16:33:35 and hit
    `csinode ... not found` while attaching (last seen 16:34:20; the event count was not captured). It attached
    at 16:34:53 and started at 16:34:55, about 22 minutes after it went down. Two things nothing tested: that
    the pod would have stayed `Pending` without the fix, and that a non-upgrade node replacement with the
    driver off would fail the same way. The version change itself played no part; the upgrade mattered only
    because it replaced the node. GKE form: the catalogue expects attach errors in `ContainerCreating`, but
    here the pod failed earlier, at scheduling. The likely mechanism is inference and nothing captured it
    during the outage. The scheduler rewrites the volume's zone rule to `topology.gke.io/zone`, and a node
    without the CSI driver lacks that label. The labels and CSINode in `19c/mechanism.txt` were recorded after
    the fix, and the `csinode ... not found` error after the fix does not fit that explanation cleanly. The
    first run (`upg-19b`) proved nothing: turning the driver off failed with a Compute Engine stockout, the
    driver stayed on, and the disk re-attached through CSI. The script now stops if the driver stays on.
20. `upg-20d` in `us-central1-c`, with a deleted tag standing in for a retired registry. The pod pulled
    `pause:3.9` from a private Artifact Registry repository onto the 1.34 node at 15:50:22. The delete of the tag
    and its index digest was issued at 15:51:29, and by 15:52:03 the repository listed no tags. The untagged
    per-architecture digests are still there. The control ran next: restarted on the same node, the pod was
    Running again at 15:52:08 with `already present on machine`, so nothing on the running cluster showed the
    image was gone. The 1.35 pool upgrade brought up a new node (registered around 16:02:52) and moved the pod
    onto it at 16:03:15. There it went to `ImagePullBackOff` with `NotFound ... pause:3.9: not found`, which is a
    missing tag, not a permissions error. What broke it was the node replacement, not version 1.35. The
    scenario's check of the node's image cache listed nothing, so the kubelet's `already present` event is the
    only evidence of the cache. No registry hostname was actually retired, and the catalogue's egress-allowlist
    variant was not tested. The earlier runs (20 to 20c) tested nothing: the node's default service account
    had no read on the repository, so the old node never cached the image.

## How the scenarios were run

Each scenario got its own throwaway GKE Standard cluster, named `upg-NN`. A letter suffix marks a re-run or a later leg (`upg-03b`,
`upg-11b`, `upg-18m`). An `h` suffix marks a hold cluster that is
never upgraded (`upg-06h`, `upg-08h`, `upg-16h`). `upg-19c` and `upg-20d` are in `us-central1-c` because
`us-central1-a` ran out of L4 GPUs and then of e2 capacity. Scenario 18 needs a GPU and chased capacity across
eleven more zones; the runs that count, `upg-18m` and `upg-18k`, are on T4s in `us-west1-a` and `us-central1-b`. The rest are in `us-central1-a`.

The verdicts rest on `scenarios/NN.sh` for each scenario and on these variants: the holds `06h`, `08h` and
`16h`; the later legs `10b`, `11b`, `13b` and `14c`; and for scenario 18, `18i` plus `18k`, which `18m` repeats
in another zone. `19c` and `20d` re-run `19` and `20` in `us-central1-c`. A variant runs as `bash run.sh 13b`,
and its evidence goes under its own name (`evidence/13b/`). A few variants extend a cluster an earlier run left
up, and name it in their header comment (`CLUSTER=upg-10 bash run.sh 10b`).

`run.sh NN` does the following:

1. Creates the cluster at the minor the scenario needs and, when the scenario sets `POOL_FLAGS`, adds a
   `work-pool`.
2. Plants the defect and records the before-state.
3. Breaks it. Twelve scenarios upgrade the control plane and then a node pool; scenario 13 upgrades only a
   node pool (a patch inside 1.31). Scenarios 05, 06, 09, 10 and 11 upgrade only the control plane. Scenarios 14 and 15 show the symptom with no upgrade; 14c then asks for
   the pool upgrade and runs the migration GKE demands.
4. Records the after-state.

Every observation goes through `ev()` in `common.sh`, which appends the command, its full output, its exit code
and a UTC timestamp to `evidence/<track>/<step>.txt`. Setup steps (credentials, manifest applies) do not. The
availability pollers write `<step>-availability.txt` and `zonal-api-api.txt` directly. A re-run adds new files
beside the checked-in ones; commit only a file a table row quotes, after replacing the project ID and number.
Redirect the console to `logs/<track>.log` if you want it; `logs/` is gitignored.

Verdicts were judged by hand from those files. Each one then went to an independent reviewer told to refute it,
who checked three things:

- The quote appears word for word.
- The quoted line comes after the trigger.
- The label does not claim more than the evidence shows.

Four labels (10, 14, 16, 18) and six quotes (1, 5, 7, 9, 10, 14) changed in that pass. The section above gives
each one's current form.

The Recommender side has two parts:

- **Holds.** `hold.sh NN` (and the `06h`, `08h` and `16h` scenarios) keep a hazard planted in its **before**
  state, with no upgrade. GKE's Recommender looks at a cluster as it is at refresh time, and a cluster that has
  already broken and moved on shows it nothing to warn about.
- **The read.** `check-recommender.sh` reads every `google.container.DiagnosisInsight` insight and every
  `google.container.DiagnosisRecommender` recommendation in each zone. It saves the raw JSON under
  `evidence/recommender/<stamp>/`, and writes `recommender.json`, which maps each insight to the cluster in its
  resource path.

A scenario counts as **caught** when an insight subtype that GKE documents for that hazard was published on a
cluster carrying the hazard. An unrelated insight on the same cluster does not count. Both catches so far came
from clusters that carry the hazard outside this campaign: `gemma-gpu` and `gemma-gpu-upgraded` for the budget,
and those two plus `seeded-a` for unenforced policies. No `upg-*` cluster has been through a refresh yet.

GKE documents a subtype for six of the twenty hazards:

| #   | Documented subtype                                                                   |
| --- | ------------------------------------------------------------------------------------ |
| 1   | `PDB_UNPERMISSIVE`                                                                   |
| 6   | `DEPRECATION_K8S_1_32_API`                                                           |
| 7   | `K8S_ADMISSION_WEBHOOK_UNAVAILABLE`                                                  |
| 10  | `CLUSTER_VERSION_SKEW_UNSUPPORTED`                                                   |
| 13  | `DEPRECATION_CONTAINERD_V1ALPHA2_CRI_API`, `DEPRECATION_CONTAINERD_V1_SCHEMA_IMAGES` |
| 16  | `NETWORK_POLICIES_UNRECONCILED`                                                      |

The other fourteen have no documented subtype. The nearest thing for scenario 14 is not an insight at all:
`gcloud` prints `Node pool ... is running cgroupv1 which is deprecated` when it touches a cgroup v1 pool. The
`gemma-gpu` cluster has run the scenario 6 caller for five days. It carries about 144 audited removed-release
writes a day, and `DEPRECATION_K8S_1_32_API` has not appeared on them yet.

## Attempts set aside

Each attempt below did not test what it meant to test. No verdict rests on it, and neither its evidence nor
the scripts that only served it (the other GPU zones, for example) are in this directory.

| Directory                                                                                                           | Why it was set aside                                                                                                                                                                                                    |
| ------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `02-attempt1-preempted-by-system-pods`, `04-attempt1`, `12-attempt1`                                                | e2-small nodes: GKE's system pods preempted the planted pods, so the upgrade was not the only thing moving them                                                                                                         |
| `03-attempt1`                                                                                                       | the placement manifest failed to parse (a missing brace in the helper), so nothing was planted                                                                                                                          |
| `04-attempt2-master-upgrade-refused`                                                                                | GKE refused the master upgrade while another operation ran (`incompatible operation`), so the pool upgrade was refused for being ahead of the master; `retry_busy` in `common.sh` now waits those out                   |
| `07-hold-attempt1-kubeconfig-corrupt`, `13-hold-attempt1-kubeconfig-corrupt`, `14-hold-attempt1-kubeconfig-corrupt` | the shared kubeconfig was corrupted mid-run (see the last section), and the re-plant's reads failed                                                                                                                     |
| `11b-attempt1-no-ip-space`                                                                                          | the default network had no room for another /14 pod range, so the cluster was never created                                                                                                                             |
| `14-attempt1-node-too-small`, `15-attempt1-node-too-small`                                                          | e2-small left no room for the 256Mi test pods                                                                                                                                                                           |
| `16-attempt1-enforcement-dormant`                                                                                   | switching enforcement on did not rebuild the node, so there was no break; it became the control for run 16                                                                                                              |
| `17-attempt1`                                                                                                       | the stand-in agent never listened (busybox `nc` has no `-q`), so the client failed before the upgrade                                                                                                                   |
| `18-attempt1`                                                                                                       | CUDA devel images on a 32 GB disk were evicted for ephemeral storage                                                                                                                                                    |
| `18` (`upg-18b`, `us-central1-a`)                                                                                   | the 1.32 round ran; the 1.33 pool upgrade (`maxSurge 0`) then deleted the only L4 node and GKE could not create another (`ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS` in `18/stockout.txt`), so there was no after-state |
| `18c` (`us-central1-c`)                                                                                             | the first L4 node was never created: `GCE_STOCKOUT` after 35 minutes                                                                                                                                                    |
| `18d` (`us-east4-c`), `18e` (`us-west1-b`), `18f` (`europe-west4-b`)                                                | L4 stockouts at pool creation (`ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS` in each `stockout.txt`)                                                                                                                      |
| `18g` (`asia-southeast1-b`)                                                                                         | got its L4 node, and was stopped at 16:29 because `18i`'s T4 node was already running the first round                                                                                                                   |
| `18j` (`europe-west1-b`)                                                                                            | a T4 hedge for `18i`; T4 stockout at pool creation (`18j/stockout.txt`)                                                                                                                                                 |
| `18n` (`europe-west4-a`, T4), `18p` (`asia-east1-a`, T4), `18q` (`asia-southeast1-b`, L4)                           | launched beside `18k` and `18m` for the 1.33 to 1.34 leg; each stocked out at pool creation (`stockout.txt`) and was stopped once `18m` finished                                                                        |
| `19-attempt1-driver-forced-on`                                                                                      | GKE turned the PD CSI driver on at create although `--addons` left it out                                                                                                                                               |
| `20-attempt1-push-failed`, `20-attempt2-crane-dyld`                                                                 | the image copy failed: Cloud Build could not push, then the released crane binary aborted on macOS                                                                                                                      |
| `20-attempt3-pool-small-and-already-upgraded`                                                                       | the pool was e2-small and already on the target version, so there was nothing to upgrade                                                                                                                                |

`19` and `20` hold attempts that ran to the end without their precondition: in `19` the PD CSI driver stayed
on, and in `20` the old node never cached the image. No verdict rests on them. `10`, `11`, `13` and `14` are earlier legs that the lettered runs extend: three-minor
skew, a read-only probe, a runtime already on containerd 2.0, and the symptom without the v1 pool.

## What the catalogue did not predict

These showed up along the way. Each was recorded in the evidence file named beside it; only the files the
table quotes are checked in, so most of these are named for the record rather than for reading here.

- GKE reports a node-pool upgrade DONE while the drained workload is still down: Pending in `01` and `12`,
  unreachable in `17`. Nothing in the operation's status says so.
- A stockout during a `maxSurge 0` GPU pool upgrade leaves the pool in ERROR with no GPU node, while the upgrade
  operation reads DONE with the Compute Engine error in its status message. This happened three times: an L4 pool in
  `us-central1-a` (`18/stockout.txt`), a T4 pool in `us-east1-d` on the 1.34 step (`18i/stockout.txt`), and
  `gemma-gpu-upgraded` earlier. The old node is gone before the shortage shows.
- GPU capacity, not quota, decided where scenario 18 could run. On 2026-09-29 L4 stocked out in six zones and
  T4 in four (the attempts table and `18i/stockout.txt`), with the regional quota nearly unused.
- On the `default` driver setting, the 1.33 to 1.34 upgrade raised the GPU driver from 535.309.01 to 580.173.02
  (`18k`, `18m`). What broke was a pod forcing NVIDIA's forward-compatibility libraries, which the newer driver
  refuses (`Error 803`). The catalogue's direction, a driver too old for the image, was already there before the
  upgrade, and the upgrade fixed it.
- An in-tree disk whose CSI driver is off failed at scheduling, not at attach (`19c`): the replacement pod stayed
  `Pending` on node affinity, which is not where the catalogue says to look.
- containerd 2.0 arrived inside the 1.31 patch line (`13`). A patch upgrade, not a minor one, is what moves
  the runtime.
- The zonal control plane showed no measurable gap for reads or writes during its own upgrade (`11`, `11b`,
  `17`).
- NetworkPolicy enforcement stays dormant until the nodes are rebuilt (`16-attempt1`). The break arrives with
  the next unrelated node upgrade.
- GKE turns the PD CSI driver on at create even when `--addons` omits it (`19-attempt1`). Turning it off can
  fail on a Compute Engine stockout (`19`), which suggests the change recreates nodes.
- GKE accepted three- and four-minor control-plane-to-node skew without refusing the control-plane upgrade,
  and left `work-pool` at 1.31 (`10`, `10b`). It did move `default-pool` to 1.34 on its own between the two
  runs, so it may still auto-upgrade a skewed pool later.
- A cgroup v1 pool can still be created on a cluster created at 1.34, but it cannot be upgraded to 1.35 (`14`,
  `14c`). This contradicts the test plan's note that such pools are refused after 1.26.
- The default compute service account on a node pool has no read on a new Artifact Registry repository (`20`).
- On e2-small nodes, GKE's system pods preempt workload pods (the first wave, above). Test pools need
  e2-standard-2 or larger.
- The Recommender's daily refresh lagged: at 17:47 on 2026-09-29, across all twelve zones, the newest was still
  2026-09-28T00:00:00Z.

## Re-reading and cleaning up

```bash
export PROJECT=<PROJECT_ID>                       # every script requires it; nothing defaults to a project
bash run.sh 01                                    # scenario 1 on cluster upg-01 in us-central1-a
ZONE=us-central1-c CLUSTER=upg-XX bash run.sh NN  # run a scenario in another zone, or under another name
CLUSTER=upg-XX bash hold.sh NN                    # re-plant a hazard in its before-state
bash check-recommender.sh && python3 results.py   # fresh Recommender read, every zone used; re-render the table
```

`check-recommender.sh` saves the raw responses under `evidence/recommender/<stamp>/`, which is gitignored
because every resource name in them carries the project number. `recommender.json` keeps the subtypes and
refresh times `results.py` reads, with the number replaced by a placeholder.

`common.sh` exports a per-cluster `KUBECONFIG` under `.kubeconfigs/`, which is gitignored. Keep it that way.
During this campaign, several `gcloud container clusters create` and `get-credentials` calls wrote the shared
`~/.kube/config` at once and corrupted it. That blanked the reads of the runs above for about three minutes.

Everything the campaign created carries the label `purpose=upgrade-scenarios`:

```bash
gcloud container clusters list --project "$PROJECT" --filter='resourceLabels.purpose=upgrade-scenarios' --format='value(name,location)'
```

Scenario 19's persistent disk (`<cluster>-intree`, in the cluster's zone) and scenario 20's Artifact Registry
repository (`upg-scenarios`, in `us-central1`) are not labelled, so delete them by hand.
