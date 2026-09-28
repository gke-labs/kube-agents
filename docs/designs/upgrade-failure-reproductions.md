# Reproducing the upgrade failure catalogue: how each scenario is planted, broken, verified, detected and fixed

This is the test plan for [`upgrade-failure-catalogue.md`](upgrade-failure-catalogue.md). The
catalogue says what breaks and which signal shows it; this document says, for each of its twenty
entries, how to create the failure in a test environment, how to make the upgrade break it, how to
prove the break happened, and the concrete check and fix on each side of the upgrade. Where the
seeded fleet already plants the scenario, or an open pull request does, the entry says so; where
it cannot live in the fleet, the entry names the out-of-tree cluster or the simulation that stands
in; and where nothing can create it on demand, the entry says what to observe and when.

## For a reader who does not run Kubernetes

A test plan for upgrades has to do something unusual: build things that are deliberately wrong,
upgrade them, and watch them fail, so that the checks meant to warn people can be shown to work.
Each entry below is one deliberate mistake. It says how to set the mistake up on a throwaway
cluster, what to do to trigger the failure (usually, start the upgrade), what evidence proves the
failure was real and not a coincidence, how a check could have seen it coming, how a check
recognises it afterwards, and what someone would change to prevent or repair it. Some mistakes are
cheap to plant in the shared test clusters every test run uses; some need a special cluster, for
example one with a graphics card or one running an old version; and a few cannot be arranged on
purpose at all, only watched for.

## The list

Each line gives the scenario, where it can be created, and what exists for it today.

1. [A PodDisruptionBudget forbids the eviction](#1-a-poddisruptionbudget-forbids-the-eviction):
   seeded fleet, in an open pull request. Fleet main: no drain-blocking budget. an open pull request
   adds readiness-drain-blocked on seeded-b. gemma-gpu measured the stall twice to the minute; its
   audit rows re-read 2026-09-28. Read today by --readiness.
2. [No spare capacity for the displaced pods](#2-no-spare-capacity-for-the-displaced-pods): an
   out-of-tree cluster. No fleet role on main. an open pull request adds readiness-surge-blocked:
   no_surge_pool on seeded-b, the maxUnavailable 1 half; main's pinned-inference-pool is the ceiling
   half. Out-of-tree: gemma-gpu scripts, once.
3. [Every replica in one zone or on one node](#3-every-replica-in-one-zone-or-on-one-node): seeded
   fleet, in an open pull request. Fleet role in an open pull request (zonal-skew-scheduling;
   no bench case, not in nightly-cases.txt); nothing on main (checkout-gateway has a hostname
   spread); no out-of-tree script; 3.7/3.8 read templates, none reads placement.
4. [Data on the node is gone](#4-data-on-the-node-is-gone): seeded fleet, a new role to plant.
   Nothing planted: no fleet role on main or in an open pull request, no out-of-tree script.
   Incidental only: gemma-gpu's vLLM keeps its weights cache in an emptyDir (model-cache, 40Gi);
   both measured upgrades refilled it.
5. [Maintenance window too short, or an exclusion ends
   mid-roll](#5-maintenance-window-too-short-or-an-exclusion-ends-mid-roll): an out-of-tree cluster.
   Covering exclusion: fleet role on main (seeded-b hold-the-minor-lag, case
   upgrades-fleet-readiness-exclusion) and gemma-gpu setup.sh. Window-too-short and
   exclusion-ends-mid-change: nothing; no script, no case, no check.
6. [A served API version is removed](#6-a-served-api-version-is-removed): an out-of-tree cluster.
   Out-of-tree only: gemma-gpu scripts; break measured once (gemma-gpu-upgraded, 2026-09-24);
   gemma-gpu still on 1.31 with the live caller. Fleet: deprecated-api-caller merged on seeded-a
   (Endpoints). No bench case yet.
7. [A fail-closed webhook whose backend is not
   up](#7-a-fail-closed-webhook-whose-backend-is-not-up): seeded fleet, in an open pull request.
   Fleet role readiness-failclosed-webhook (seeded-b) in an open pull request, applied nowhere
   2026-09-28; no eval case reads it; no out-of-tree script; gemma-gpu plants no webhook; readiness
   mode reads PDBs, exclusions, skew
8. [A default changes in the new minor](#8-a-default-changes-in-the-new-minor): an out-of-tree
   cluster. Nothing exists: no fleet role on main or in an open pull request; gemma-gpu plants a removed API and a
   drain block, not a flipped default. gemma-gpu-upgraded (cp 1.32.13, default-pool 1.31.14,
   measured) is a ready host.
9. [A feature is deprecated but still served](#9-a-feature-is-deprecated-but-still-served): seeded
   fleet, planted today. Fleet role deprecated-api-caller on main, applied and yielding in the dev
   copy (seeded-a v1.35.8, 144 stamps in 24 h, no Failed job); no eval case reads it yet.
   externalIPs: nothing; throwaway-only on GKE. IPVS: none.
10. [Add-on and client skew](#10-add-on-and-client-skew): an out-of-tree cluster. Nothing plants it:
    no fleet role or open pull request (seeded-b's pool is pinned to its master; main.tf says why); gemma-gpu
    starts pool and master equal. Pool skew: upgrade_readiness.py evaluate_skew; add-on/client unread
11. [The control plane is unreachable for minutes on a zonal
    cluster](#11-the-control-plane-is-unreachable-for-minutes-on-a-zonal-cluster): an out-of-tree
    cluster. No fleet role, no open pull request, nothing reads location type. All fleet slots and gemma-gpu
    are zonal; break-upgrade.sh + watch-break.py ran one master upgrade on 2026-09-24, no gap seen at
    60 s.
12. [A node label or taint is removed](#12-a-node-label-or-taint-is-removed): simulation only.
    Nothing in tree. No fleet role; an open pull request's readiness-pinned-workload pins
    pinned-batch-runner to a tainted pool, not a vanishing label. gemma-gpu's scripts do not plant
    it; gemma-gpu-upgraded's default pool serves.
13. [The container runtime changes](#13-the-container-runtime-changes): an out-of-tree cluster.
    Nothing exists: no fleet role on main or an open pull request; gemma-gpu's 1.31 to 1.32 path keeps
    containerd 1.7; the one nearby reader, security-patch orchestrator 3.9 stale-image-type, passes
    COS_CONTAINERD and stays silent.
14. [cgroup v2 under a runtime that cannot read
    it](#14-cgroup-v2-under-a-runtime-that-cannot-read-it): an out-of-tree cluster. Nothing exists:
    no fleet role, open pull request or gemma-gpu script. Every gemma-gpu and gemma-gpu-upgraded pool reads
    EFFECTIVE_CGROUP_MODE_V2 today; no kube-agents script reads effectiveCgroupMode or images.
15. [The OOM killer starts killing the whole
    container](#15-the-oom-killer-starts-killing-the-whole-container): simulation only. Nothing
    exists: no fleet role (seeded-a/c 1.35.8, seeded-b 1.34.11, all past the 1.33 migration), no open pull request, no gemma-gpu script; the four gemma pools read V2,
    opt-out unset (2026-09-28).
16. [The network dataplane changes](#16-the-network-dataplane-changes): simulation only. Nothing
    planted: fleet Terraform sets no datapath, DNS or policy fields; gemma-gpu, gemma-gpu-upgraded
    and seeded-a/b/c read legacy, kube-dns, addon disabled (2026-09-28); gemma-gpu has 1 unenforced
    policy; no open pull request.
17. [A node networking agent fails on the new
    image](#17-a-node-networking-agent-fails-on-the-new-image): simulation only. Nothing exists
    today: no fleet role on main or an open pull request; gemma-gpu scripts cover entries 1, 2 and 6 only;
    the catalogue reads 'Read today: nothing', no Recommender insight. Scenario 12 plans the same
    event.
18. [GPU driver mismatch](#18-gpu-driver-mismatch): an out-of-tree cluster. Nothing plants it.
    Fleet: GPU excluded. gemma-gpu (setup.sh) has one L4 pool on gpu-driver-version=latest, no
    mismatch. Repo: prose only (gke-upgrades SKILL.md 148-153, troubleshooting.md section 9,
    checklists.md 27).
19. [In-tree volumes lose their CSI path](#19-in-tree-volumes-lose-their-csi-path): an out-of-tree
    cluster. Nothing exists: no fleet role on main or an open pull request, no out-of-tree script (gemma-gpu
    plants API removal and drain, not storage), no kube-agents reader; the catalogue says 'Read
    today: nothing'. This recipe is first.
20. [Images on a retired registry](#20-images-on-a-retired-registry): seeded fleet, a new role to
    plant. Nothing: no fleet role on main or in the open pull requests, no out-of-tree script. gemma-gpu
    pulls from us-docker.pkg.dev and docker.io; its only egress control is a pod NetworkPolicy, which
    kubelet pulls bypass.

## The scenarios

Each section has the same parts. Plant: the resources or commands that create the precondition.
Break: the upgrade or event that turns it into a failure, with the expected timeline. Verify: the
observation that proves the failure happened. Detect before and after: the concrete read, its
assertion, and which kube-agents component reads it today. Fix before and after: the concrete
change. Then cost and time, what exists today, and caveats.

### 1. A PodDisruptionBudget forbids the eviction

Home: seeded fleet, in an open pull request. Plant: an open pull request adds
readiness-drain-blocked on seeded-b: PDB pinned-batch-runner, maxUnavailable 0, one pause replica
on no-surge-pool. Break: never in the fleet (read-only, pool pinned to the master); use a
disposable cluster or gemma-gpu.

- Plant:
  - Fleet (an open pull request, bench/tf/fleet/defects-b.tf): kubernetes_pod_disruption_budget_v1
    "drain_blocked" on seeded-b, ns seeded-upgrade, name pinned-batch-runner, max_unavailable = "0",
    selector app=pinned-batch-runner; fixtures.json role readiness-drain-blocked.
  - Its target (same file): kubernetes_deployment_v1 "pinned_batch_runner", replicas = 1, image
    registry.k8s.io/pause:3.10, node_selector seeded-role=no-surge onto no-surge-pool (max_surge 0 /
    max_unavailable 1, version = local.lagging_version = the master's).
  - Disposable, no GPU: OLD = older REGULAR validVersions entry, NEW = defaultVersion (gcloud
    container get-server-config --location Z); gcloud container clusters create pdb-drain --zone Z
    --release-channel regular --cluster-version $OLD --num-nodes 1
  - gcloud container node-pools create no-surge --cluster pdb-drain --zone Z --num-nodes 1
    --machine-type e2-small --max-surge-upgrade 0 --max-unavailable-upgrade 1 --node-labels
    role=no-surge (system pods stay on default-pool, as on gemma-gpu and the fleet).
  - kubectl create deploy pause --image=registry.k8s.io/pause:3.10 --replicas=1; kubectl patch
    deploy pause -p '{"spec":{"template":{"spec":{"nodeSelector":{"role":"no-surge"}}}}}'; kubectl
    create pdb pause --selector=app=pause --max-unavailable=0
- Break:
  - Disposable: gcloud container clusters upgrade pdb-drain --master --cluster-version $NEW --zone Z
    --quiet (~10 min, break-upgrade.sh step 1), then the same command with --node-pool no-surge
    --async instead of --master. gemma-gpu: ./break-upgrade.sh.
  - Measured twice on gemma-gpu: T+0 UPGRADE_NODES starts; T+1.5 min node Ready,SchedulingDisabled,
    pod Running and serving; GKE retries the eviction every 2 s (1,710 refusals in run 2); T+60 pod
    deleted; node back ~T+63; DONE T+64 to T+66, workload not back.
  - Never against seeded-b: read-only for cases, pool pinned to the master version, an hour-long
    hold. On the disposable cluster, kubectl drain <node> --ignore-daemonsets --timeout=60s shows
    the refusal within a minute; kubectl uncordon <node> undoes the cordon.
- Verify:
  - kubectl get node -o
    custom-columns='NAME:.metadata.name,UNSCHED:.spec.unschedulable,VER:.status.nodeInfo.kubeletVersion'
    shows unschedulable true on the old version; kubectl get pod -n <ns> -o wide keeps the matched
    pod Running on that node for ~60 min.
  - An eviction by hand is refused: kubectl create --raw /api/v1/namespaces/<ns>/pods/<pod>/eviction
    -f - with a policy/v1 Eviction body returns 429 'Cannot evict pod as it would violate the pod's
    disruption budget' (break-upgrade.sh; kubernetes.io api-eviction).
  - gcloud container operations list --zone Z --filter='operationType=UPGRADE_NODES AND
    status=RUNNING' --format='table(name,startTime)' stays RUNNING past 60 min for a one-node pool;
    after the kill it reads DONE while kubectl rollout status deploy/<d> waits.
  - Audit trail: gcloud logging read 'log_id("cloudaudit.googleapis.com/activity") AND
    resource.labels.cluster_name="<c>" AND
    protoPayload.methodName="io.k8s.core.v1.pods.eviction.create" AND
    protoPayload.response.code=429' --freshness 2h: one robot row per 2 s.
- Detect before:
  - kubectl get pdb -A -o
    custom-columns='NS:.metadata.namespace,NAME:.metadata.name,MAXU:.spec.maxUnavailable,MINA:.spec.minAvailable,ALLOWED:.status.disruptionsAllowed';
    blocked when maxUnavailable is 0/0% or minAvailable >= the owner's replicas; decide on spec.
  - Read today: fleet_upgrade_report.py --readiness --kubeconfig-dir <dir> (rule 1 of 3, SOP §3.4)
    grades it blocked, naming ns/name, field and workloads; obtainability 3.4 blocking-pdb files it
    critical; an open pull request adds upgrade SOP 3.11 upgrade-blocked.
  - gcloud recommender insights list --project P --location Z --insight-type
    google.container.DiagnosisInsight --filter insightSubtype=PDB_UNPERMISSIVE;
    content.podDisruptionInsight[].pdbInfo names the budget; daily; gemma-gpu flagged within a day;
    human-read.
- Detect after:
  - Stall: gcloud container operations list --zone Z --filter='operationType=UPGRADE_NODES AND
    status=RUNNING' --format='value(name,startTime)', startTime older than ~15 min per node. On the
    agent's allowlist, unread; --rollout-in-progress sees only version stall.
  - Cause, live: the verify audit query returns a 429 row every 2 s from
    service-<project-number>@container-engine-robot.iam.gserviceaccount.com; kubectl get pdb -n <ns>
    <name> -o jsonpath='{.status.disruptionsAllowed}' is 0, currentHealthy >= 1. Allowed, unread.
  - Aftermath: audit row protoPayload.methodName="io.k8s.core.v1.pods.delete" by the same robot at
    T+60 (measured 19:05:44); DONE while kubectl rollout status deploy/<d> is not complete (23-24
    min on gemma-gpu); PDB_UNPERMISSIVE still listed next refresh. Unread.
- Fix before:
  - Give the budget room: kubectl patch pdb <name> -n <ns> --type merge -p
    '{"spec":{"maxUnavailable":1}}' (GitOps: edit the manifest; obtainability 3.4 emits this rewrite
    at replicas >= 2) and run 2 replicas so it holds.
  - True singleton: keep the budget, upgrade in the maintenance window, accept the outage;
    blue-green (gcloud container node-pools update <pool> --enable-blue-green-upgrade) also honours
    it for one hour, then deletes.
  - Shorten the outage after the kill: gcloud container node-pools update <pool> --cluster C
    --max-surge-upgrade 1 --max-unavailable-upgrade 0: a replacement node exists before the pod dies
    (needs a second machine/GPU).
- Fix after:
  - During the stall: apply the patch above; GKE retries every 2 s, so the drain resumes at once.
    With minAvailable == replicas, kubectl scale deploy/<d> --replicas=<n+1> frees a disruption once
    the new pod is Ready.
  - Never delete the budget without a replacement (a stall becomes an unprotected workload). After
    the force-kill, fix the budget before the next node; kubectl rollout status deploy/<d> says when
    it is back, DONE does not.

- Cost and time: Disposable e2-small: ~10 min create, ~10 min control-plane step, ~65 min stall +
  rebuild, cents/hour; gemma-gpu copy: L4 + EXTENDED fee, 23-24 min outage.
- Exists today: Fleet main: no drain-blocking budget. an open pull request adds
  readiness-drain-blocked on seeded-b. gemma-gpu measured the stall twice to the minute; its audit
  rows re-read 2026-09-28. Read today by --readiness.
- Caveats:
  - seeded-a's inference-server PDB (maxUnavailable 1) also sits at disruptionsAllowed 0 because its
    surplus replicas are Pending; the readiness rule decides on spec, so it is not graded blocked
    (defects-a.tf says so).

### 2. No spare capacity for the displaced pods

Home: an out-of-tree cluster. The break is a node-pool upgrade, forbidden in the read-only fleet, so
plant and break run in a user project. The fleet holds before-signals: an open pull request's
no-surge-pool (maxSurge 0 / maxUnavailable 1) on seeded-b, main's pinned-inference-pool
(autoscaler 1/1).

- Plant:
  - CPU variant, any Standard zonal cluster you own: gcloud container node-pools create no-headroom
    --cluster $C --zone $Z --machine-type e2-small --num-nodes 2 --enable-autoscaling --min-nodes 2
    --max-nodes 2 --max-surge-upgrade 0 --max-unavailable-upgrade 1
  - Add --node-labels pool=no-headroom --node-taints pool=no-headroom:NoSchedule (only DaemonSets
    land, as the fleet's pinned pool relies on) and --node-version <one patch below
    currentMasterVersion, from get-server-config channels[].validVersions>.
  - Read free CPU first: kubectl describe node <n> | grep -A5 'Allocated resources' (e2-small
    allocates 940m; DaemonSets take ~250m per bench/tf/fleet/defects-a.tf). Request more than half
    of what is free, e.g. cpu 500m: one pod fits a node, two do not.
  - Fill both nodes: a Deployment, replicas 2, nodeSelector pool=no-headroom, toleration
    pool=no-headroom:NoSchedule, one pause container with that request. No PDB: a budget is entry 1.
    Autoscaler max equal to node count is the ceiling signal.
  - GPU variant, scripted: gke-fleet-iac/clusters/gemma-gpu/setup.sh builds gpu-pool (one
    g2-standard-4 + L4, --max-surge-upgrade 0 --max-unavailable-upgrade 1, no autoscaler) and the
    one-replica vLLM pod; then kubectl -n kubeagents-system delete pdb gemma-server.
- Break:
  - gcloud container clusters upgrade $C --node-pool no-headroom --cluster-version
    <currentMasterVersion> --zone $Z --quiet --async, as break-upgrade.sh does. Watch first:
    watch-break.py, its gpu-pool and app=gemma-server selectors edited for the CPU pool.
  - CPU timeline: eviction succeeds at once (no budget); GKE cordons, drains and recreates node 1 in
    place; its pod is Pending with Insufficient cpu until node 1 is back (gemma-gpu run 1 rebuilt a
    node in 3 min: killed 16:02:00, node 16:05:11); then node 2; DONE.
  - GPU, measured 2026-09-24 on gemma-gpu-upgraded after the budget's force-kill at 19:05:44: only
    L4 node destroyed; MIG retried every 2 min from 19:08:39, ZONE_RESOURCE_POOL_EXHAUSTED, pool
    ERROR; op DONE 19:10:44, no GPU node; L4 back 19:16:54; Ready 19:28:52.
- Verify:
  - kubectl get events -A --field-selector reason=FailedScheduling --sort-by=.lastTimestamp: a
    message with 'Insufficient cpu' (GPU: 'Insufficient nvidia.com/gpu') for the displaced pod;
    kubectl get pods -A --field-selector status.phase=Pending lists it meanwhile.
  - gcloud container operations list --zone $Z --filter='targetLink~$C AND
    operationType=UPGRADE_NODES': RUNNING then DONE. DONE means nodes at the version, not pods back:
    run 1 DONE 16:05:56, scheduled 16:06:02, Ready 16:26:22; run 2 DONE with the pool in ERROR.
  - Stockout evidence, GPU only, when it happens: gcloud compute instance-groups managed list-errors
    <MIG> --zone $Z lists ZONE_RESOURCE_POOL_EXHAUSTED (MIG from node-pools describe
    --format='value(instanceGroupUrls)'). CPU variant: no MIG error, no ERROR pool.
  - Proof the pool did not surge: kubectl get nodes -l cloud.google.com/gke-nodepool=<pool> -w never
    shows N+1 nodes; one node goes SchedulingDisabled and vanishes before its replacement is Ready,
    unlike a maxSurge 1 roll where the new node appears first.
- Detect before:
  - node-pools describe <pool>
    --format='value(upgradeSettings.maxUnavailable,upgradeSettings.maxSurge,autoscaling.maxNodeCount)';
    flag maxUnavailable > 0 (empty maxSurge is 0). Unread: upgrade_readiness.py (--readiness) grades
    PDBs, exclusions and skew only.
  - Headroom: sum cpu, memory, nvidia.com/gpu requests of pool pods (kubectl get pods -A -o json,
    grouped by spec.nodeName) against kubectl get nodes -o jsonpath='{.items[*].status.allocatable}'
    times (nodes - 1); flag when one node's pods do not fit. Unread.
  - Ceiling and quota: autoscaling.maxNodeCount equal to node count; gcloud compute regions describe
    $R --format='json(quotas)', usage near limit for NVIDIA_L4_GPUS or CPUS. Read today: stockout
    audit 3.9 single-zone-nodepool (maxNodeCount), 3.7 quota-exhaustion.
- Detect after:
  - kubectl get pods -A --field-selector status.phase=Pending and events reason=FailedScheduling
    with 'Insufficient cpu' or 'Insufficient nvidia.com/gpu' after DONE, against a Pending count
    taken before. Unread: specified in upgrade-readiness-checks.md, not built.
  - Autoscaled pools only, not gemma-gpu:
    log_id("container.googleapis.com/cluster-autoscaler-visibility"),
    jsonPayload.noDecisionStatus.noScaleUp or resultInfo.results.errorMsg.messageId
    scale.up.error.out.of.resources / .quota.exceeded. Stockout audit 3.11, 24h.
  - node-pools describe <pool> --format='yaml(status,conditions)' reads status ERROR (measured;
    documented condition codes include GCE_STOCKOUT, GCE_QUOTA_EXCEEDED) while operations describe
    <op> says DONE. Unread; DONE alone reports success mid-outage.
- Fix before:
  - Back to GKE's defaults: gcloud container node-pools update <pool> --cluster $C --zone $Z
    --max-surge-upgrade 1 --max-unavailable-upgrade 0; GKE creates the replacement first and waits
    for capacity, so the old node stays.
  - Give surge room: node-pools update <pool> --enable-autoscaling --min-nodes N --max-nodes N+1, or
    gcloud container clusters resize $C --node-pool <pool> --num-nodes N+1; check regional quota
    covers one more node.
  - Keep maxSurge 0 / maxUnavailable 1 only on reserved nodes (GKE: recreating releases unreserved
    capacity; gke-upgrades prescribes it for fixed reservations, blue-green needs 2x), in a
    maintenance window, one rebuild down.
- Fix after:
  - Add capacity and the Pending pods schedule: gcloud container clusters resize $C --node-pool
    <pool> --num-nodes N+1, or node-pools update --enable-autoscaling --max-nodes N+1 if autoscaled;
    then Pending is empty.
  - On a stockout no command helps: the MIG retries by itself (every 2 min when measured; the L4
    returned after 8) until the zone has stock. Otherwise a pool in another zone or machine type the
    workload can select.

- Cost and time: CPU: two e2-small under an hour, cents; a node rebuild took 3 min (run 1), the
  Pending gap per node. GPU: gemma-gpu's L4 round the clock; 23-24 min outage.
- Exists today: No fleet role on main. an open pull request adds readiness-surge-blocked:
  no_surge_pool on seeded-b, the maxUnavailable 1 half; main's pinned-inference-pool is the ceiling
  half. Out-of-tree: gemma-gpu scripts, once.
- Caveats:
  - The stockout half cannot be created on demand; the 8-minute L4 shortage was observed once, on
    2026-09-24. The CPU variant reproduces the Pending gap deterministically but never the ERROR
    pool or the MIG error.
  - maxSurge 0 / maxUnavailable 1 is what gke-upgrades prescribes for reservation-bound GPU pools
    and GKE documents for reserved nodes; flag its join with unreserved capacity, no headroom or one
    replica, not the setting.

### 3. Every replica in one zone or on one node

Home: seeded fleet, in an open pull request. an open pull request (zonal-skew-scheduling, seeded-d)
plants this shape: zone-pinned-api, two replicas, a ScheduleAnyway zonal spread and a required
single-zone nodeAffinity. The fleet is read-only, so the break step runs on a throwaway pool in
the gemma-gpu project.

- Plant:
  - Fleet (an open pull request, defects-d.tf): Deployment zone-pinned-api, ns seeded-topology,
    replicas 2, zonal topology_spread_constraint ScheduleAnyway, required node_affinity
    topology.kubernetes.io/zone In [var.zone], PDB maxUnavailable 1, requests 10m/16Mi.
  - Fleet shape: seeded-d is one e2-small per zone (node_locations=[var.second_zone], node_count 1
    per zone), so one zone is one node. Observe the co-location there; never drain or upgrade it.
    The break runs only on the out-of-tree pool.
  - Out-of-tree pool: gcloud container node-pools create colocated-pool --cluster gemma-gpu --zone
    us-central1-a --project haoxuw-gke-dev --num-nodes 1 --machine-type e2-medium --node-version
    <EXTENDED validVersion below the master's, per setup.sh's VER line>
  - Out-of-tree: Deployment colocated-api, ns kubeagents-system, replicas 2, nodeSelector
    cloud.google.com/gke-nodepool: colocated-pool, no topologySpreadConstraints, podAntiAffinity or
    PDB; nginx:1.27, requests 10m/16Mi, readinessProbe :80, initialDelaySeconds 30
  - Keep the manifest beside the scripts and kubectl apply -f it by hand: setup.sh applies every
    manifests/*.yaml with -n kubeagents-system on every CLUSTER=<copy>, and a copy without
    colocated-pool would hold these pods Pending forever.
- Break:
  - Confirm placement, then: gcloud container clusters upgrade gemma-gpu --node-pool=colocated-pool
    --cluster-version "$(gcloud container clusters describe gemma-gpu --zone us-central1-a
    --format='value(currentMasterVersion)')" --zone us-central1-a --quiet
  - Expected (pool default maxSurge 1 / maxUnavailable 0): surge node Ready first, old node cordoned
    and drained; with no budget both evictions are granted within seconds and both replacements
    start on the surge node; zero Ready pods for the 30 s delay plus pull.
  - Rehearsal without an upgrade, out-of-tree only: kubectl drain <colocated-pool node>
    --ignore-daemonsets --delete-emptydir-data makes the same Eviction API calls GKE's drain makes;
    kubectl uncordon <node> afterwards. Do not drain seeded-d.
- Verify:
  - Before: kubectl -n kubeagents-system get pods -l app=colocated-api -o
    custom-columns='POD:.metadata.name,NODE:.spec.nodeName' lists one node twice; fleet: same read,
    --kubeconfig $BENCH_FLEET_KUBECONFIG_DIR/zonal-skew-scheduling.kubeconfig -n seeded-topology
  - During: while :; do echo "$(date -u +%T) $(kubectl -n kubeagents-system get pods -l
    app=colocated-api -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}
    {end}')"; sleep 1; done; the outage is every line with no True.
  - Audit log: gcloud logging read 'resource.type="k8s_cluster" AND
    resource.labels.cluster_name="gemma-gpu" AND
    protoPayload.methodName="io.k8s.core.v1.pods.eviction.create" AND
    protoPayload.resourceName:"colocated-api"' --project haoxuw-gke-dev --freshness 1h
  - Operation: gcloud container operations list --zone us-central1-a --project haoxuw-gke-dev
    --filter='operationType=UPGRADE_NODES AND targetLink~gemma-gpu' reads DONE whatever the pods
    did; on gemma-gpu's measured run DONE came 20 min before the pod was Ready
- Detect before:
  - Placement: kubectl get pods -A -o
    custom-columns='NS:.metadata.namespace,OWNER:.metadata.ownerReferences[0].name,NODE:.spec.nodeName'
    (OWNER = ReplicaSet) and kubectl get nodes -L topology.kubernetes.io/zone; assert distinct nodes
    and zones per owner
  - Spec: obtainability 3.8 no-spread flags replicas >= 2 with neither topologySpreadConstraints nor
    podAntiAffinity (out-of-tree copy, minor); 3.7 rigid-scheduling flags a required nodeAffinity
    term with one zone (zone-pinned-api, major, declared). Read today.
  - Unread between them: the audit's dump excludes Pods on purpose and nothing reads
    spec.template.spec.topologySpreadConstraints[*].whenUnsatisfiable, so ScheduleAnyway passes 3.8
    while both pods share a zone; --readiness reads PDBs, exclusions, pool skew only.
- Detect after:
  - Outage: the per-pod Ready read in verify shows no True for the window. The Deployment's
    Available reason (MinimumReplicasUnavailable) also reads so at 1 of 2, and
    status.availableReplicas is omitted at 0, so neither proves zero alone. Unread by the watcher.
  - FailedScheduling appears only when no other node satisfies the pod (a maxSurge 0 pool, or a pin
    to the cordoned node's zone); with the default surge node, no event. kubectl get events -A
    --field-selector reason=FailedScheduling; not on the deployed list
  - The eviction audit query in verify, run over the drain, is the durable record of two evictions
    with no budget refusal between them; the GKE operation reads DONE either way, so operation
    status is not a signal for this scenario.
- Fix before:
  - topologySpreadConstraints maxSkew 1: kubernetes.io/hostname DoNotSchedule when nodes >=
    replicas, topology.kubernetes.io/zone ScheduleAnyway; labelSelector = spec.selector. SOP 3.8's
    own fix is hostname ScheduleAnyway.
  - Remove or widen the pin: drop the required nodeAffinity on topology.kubernetes.io/zone, or list
    two or more zones (3.7 does not flag a two-value term). Under nodeAffinityPolicy Honor (default)
    one zone is one domain.
  - Add a budget so the drain waits between replicas: PodDisruptionBudget maxUnavailable 1 on the
    same selector. Then kubectl rollout restart deploy/<name>: kubernetes.io says constraints are
    not kept satisfied after placing
- Fix after:
  - Add a second node, then re-spread: gcloud container clusters resize gemma-gpu --node-pool
    colocated-pool --num-nodes 2 --zone us-central1-a --quiet; apply constraints and budget; kubectl
    rollout restart; re-read.
  - GKE ships no descheduler (kubernetes.io names the Descheduler project); the restart is the
    re-spread. Teardown: kubectl -n kubeagents-system delete deploy colocated-api; gcloud container
    node-pools delete colocated-pool

- Cost and time: Out-of-tree: one e2-medium for a few hours (cents), one pool upgrade; create plus
  one-node surge upgrade typically within 30 min. Fleet: nothing beyond seeded-d
- Exists today: Fleet role in an open pull request (zonal-skew-scheduling; no bench case, not
  in nightly-cases.txt); nothing on main (checkout-gateway has a hostname spread); no out-of-tree
  script; 3.7/3.8 read templates, none reads placement.
- Caveats:
  - Fleet copy: with the one-zone pin and maxUnavailable 1, a drain's second eviction waits for a
    replacement only a surge node in var.zone can host, else GKE force-evicts after an hour. Zero
    needs the out-of-tree copy.
  - NO_MINOR_UPGRADES permits patch upgrades and gemma-gpu's window is 03:00-07:00Z daily, so a pool
    created a patch behind can be auto-upgraded overnight: create and break the same day. 10m
    requests keep 3.1 quiet and fit.

### 4. Data on the node is gone

Home: seeded fleet, a new role to plant. emptyDir half as a new seeded-a role, the before-check's
fixture; the fleet is read-only, so its break is GKE's auto-upgrade when REGULAR rolls, observed.
Local SSD and hostPath out-of-tree: Local SSD needs n1-standard-1 (no e2), hostPath trips
compliance 2.3.

- Plant:
  - Fleet role node-local-state on seeded-a, new ns seeded-node-state (kubernetes_namespace_v1; add
    it to default_deny's for_each): kubernetes_deployment_v1 "session_cache", replicas 1,
    busybox:1.36, volume { name = "cache" empty_dir {} } at /cache.
  - Pod spec as payments-api: automount_service_account_token = false, SOP 2.11 security_context
    (uid 65534, RuntimeDefault). Init: command = ["sh","-c","[ -f /cache/marker ] || echo \"$(date
    -u +%FT%TZ) $(hostname)\" > /cache/marker"]; main sleeps, 10m/16Mi.
  - fixtures.json role: cluster_slot a, namespace seeded-node-state, probes
    ["deployment/session-cache"]; state, a why each: pod?app=session-cache
    status.conditions[?(@.type=='Ready')].status any_eq "True", spec.volumes[*].name any_eq "cache";
    spec.replicas eq 1.
  - Out-of-tree (any cluster, e.g. gemma-gpu): gcloud container node-pools create ssd-pool --cluster
    C --location Z --machine-type n1-standard-1 --num-nodes 1 --ephemeral-storage-local-ssd count=1.
    GKE: n1-standard-1 or larger; emptyDir then lives on the SSD.
  - On it, apply a one-replica Deployment app=node-state as root: nodeSelector
    cloud.google.com/gke-ephemeral-storage-local-ssd: "true", emptyDir at /cache, hostPath {path:
    /var/tmp/node-state, type: DirectoryOrCreate} at /node, same seed-if-absent init for both.
- Break:
  - Out-of-tree: gcloud container clusters upgrade C --node-pool=ssd-pool --location=Z
    --cluster-version=<currentMasterVersion>: the same-version call GKE documents to recreate nodes.
    No PDB, no stall: minutes (gemma: force-kill 16:02:00, node back 16:05:11).
  - Fleet: no on-demand break; the fleet is read-only and no case may upgrade it. seeded-a is on
    REGULAR with a 03:00 UTC window: when REGULAR rolls a version, GKE rebuilds default-pool's two
    nodes one at a time (maxSurge 1), as on 2026-09-07. Observe only.
  - Repair-shaped alternative, no UPGRADE_NODES operation: gcloud compute instance-groups managed
    recreate-instances <MIG from node-pools describe --format='value(instanceGroupUrls)'>
    --instances=<node> --zone Z deletes the VM and recreates it from its template.
- Verify:
  - Before: kubectl -n NS exec deploy/session-cache -- cat /cache/marker prints '<time> <pod>'; save
    it with kubectl get pod -l app=session-cache -o jsonpath='{.items[0].metadata.uid}
    {.items[0].spec.nodeName}' and that node's .status.nodeInfo.bootID.
  - Out-of-tree control: kubectl delete pod -l app=node-state, wait Ready, cat both markers:
    /node/marker (hostPath) unchanged, /cache/marker (emptyDir) new. hostPath data survives a pod
    restart, so what the rebuild removes is node data, not pod data.
  - After: gcloud container operations list --location Z --filter='operationType=UPGRADE_NODES AND
    targetLink~ssd-pool' --format='value(status,startTime,endTime)' is DONE; node name (surge) or
    bootID changed; both markers dated after startTime; saved line gone.
  - Expected silence: kubectl get events -A --field-selector involvedObject.name=<new pod> shows
    only Scheduled, Pulled, Created, Started; nothing reports the loss. Fleet, after a roll:
    deployment creationTimestamp < node creationTimestamp <= marker time.
- Detect before:
  - kubectl get po -A -o json | jq -r '.items[]|select(.metadata.namespace!="kube-system" and
    .metadata.annotations["cluster-autoscaler.kubernetes.io/safe-to-evict"]!="true" and
    any(.spec.volumes[]?;.hostPath or .emptyDir))|.metadata.namespace+"/"+.metadata.name'
  - gcloud container node-pools list --cluster C --location Z
    --format='table(name,config.localSsdCount,config.ephemeralStorageLocalSsdConfig.localSsdCount,config.localNvmeSsdBlockConfig.localSsdCount)':
    a count >0 is node data on Local SSD. Unread.
  - kubectl get pv -o json | jq -r '.items[]|select(.spec.local)|.metadata.name+"
    "+.spec.claimRef.name': a local PV is pinned by nodeAffinity to a node the rebuild deletes;
    assert none Bound. Compliance 2.3 and cost SOP 3.8 read volumes for other ends.
- Detect after:
  - kubectl get pod -l app=X -o jsonpath='{.items[0].metadata.uid} {.items[0].status.startTime}'
    against gcloud container operations describe OP --location Z --format='value(startTime)': a pod
    started after the operation is new, and every emptyDir with it. Unread.
  - kubectl exec deploy/X -- sh -c 'ls -la /cache; cat /cache/marker': only what the new pod wrote,
    the marker dated after the operation. On a real application the same read is its data directory.
    Unread.
  - The catalogue's after-signal is the app's own errors: kubectl logs deploy/X --since-time=<op
    startTime> for read errors, empty queues, cache misses. gemma-gpu's replacement refilled its
    emptyDir model cache: container 19:22:33Z, Ready 19:28:52Z. Unread.
- Fix before:
  - State on a PVC (standard-rwo, pd.csi.storage.gke.io): GKE unmounts, not erases, persistent disks
    during upgrades; the disk reattaches in its zone. Weights and other read-mostly data: GCS via
    Cloud Storage FUSE CSI.
  - Keep emptyDir and Local SSD for what the app rebuilds; annotate
    cluster-autoscaler.kubernetes.io/safe-to-evict: "true", which the autoscaler and the jq above
    read as rebuildable; a startupProbe gates readiness on it.
  - Data that cannot move before the window: copy it out first (kubectl exec ... tar cf - /cache |
    gcloud storage cp - gs://...) and rehearse the restore. A PDB delays the drain by at most an
    hour; it saves nothing.
- Fix after:
  - Nothing on GKE brings it back: the VM, its boot disk and Local SSD go with the node. Restore
    from the source of truth (backup, upstream store, re-download); judge recovery by the pod's
    Ready, not the operation's DONE.
  - Mid-roll, gcloud container operations cancel OP --location Z: unstarted nodes keep the old
    version and their data, started nodes finish; copy the survivors out, then apply the fix_before
    change before the next window.

- Cost and time: Fleet: one 10m/16Mi busybox pod on seeded-a, no added spend; break on GKE's
  schedule. Out-of-tree: an n1-standard-1 plus a 375 GiB Local SSD, about an hour.
- Exists today: Nothing planted: no fleet role on main or in an open pull request, no out-of-tree
  script. Incidental only: gemma-gpu's vLLM keeps its weights cache in an emptyDir (model-cache,
  40Gi); both measured upgrades refilled it.
- Caveats:
  - GKE documents the same-version upgrade (Shielded GKE Nodes, maintenance-windows pages) as how to
    recreate nodes; it is a real UPGRADE_NODES roll under the pool's surge settings. MIG recreate is
    a repair, not an upgrade.
  - No read can tell rebuildable scratch from state; the check needs the safe-to-evict annotation or
    a human. hostPath stays out of the fleet (needs root, compliance 2.3 flags it) and the fleet
    break is observe-only.

### 5. Maintenance window too short, or an exclusion ends mid-roll

Home: an out-of-tree cluster. Covering-exclusion half is on main (seeded-b hold-the-minor-lag). The
window-length half needs a 5-node PDB-stalled pool and an automatic upgrade nobody can start on
demand: put the pool on gemma-gpu (setup.sh window 03:00-07:00Z); the pause is observe-only.

- Plant:
  - Window (4h, GKE's minimum 'at least 4 hours (4H)'): gcloud container clusters update $C --zone
    $Z --project $P --maintenance-window-start 2000-01-01T03:00:00Z --maintenance-window-end
    2000-01-01T07:00:00Z --maintenance-window-recurrence FREQ=DAILY (setup.sh).
  - Stall pool: gcloud container node-pools create stall-pool --cluster $C --zone $Z --num-nodes 5
    --machine-type e2-small --max-surge-upgrade 0 --max-unavailable-upgrade 1 --enable-autoupgrade
    --node-version <validNodeVersions entry one patch below the master>
  - One-hour stall per node: Deployment stall, pause image, replicas 5, nodeSelector
    cloud.google.com/gke-nodepool: stall-pool, topologySpreadConstraints maxSkew 1, topologyKey
    kubernetes.io/hostname, whenUnsatisfiable DoNotSchedule; PDB maxUnavailable: 0 on it.
  - Exclusion ending mid-change: gcloud container clusters update $C --zone $Z
    --add-maintenance-exclusion-name ends-mid-change --add-maintenance-exclusion-start <now>
    --add-maintenance-exclusion-end <in the change> --add-maintenance-exclusion-scope no_upgrades
  - Covering exclusion: already planted; seeded-b hold-the-minor-lag (NO_MINOR_UPGRADES,
    bench/tf/fleet/main.tf) and gemma-gpu hold-gpu-minor (setup.sh). Do not add one to the fleet.
    API limits: no_upgrades max 90 days, 3 per cluster, 20 total, 48h window/92 days.
- Break:
  - Window pause: cannot be forced. 'Manual upgrades bypass any configured maintenance windows and
    maintenance exclusions' (GKE docs); gemma-gpu's manual pool upgrade ran 15:01-16:05Z against a
    03:00-07:00Z window without pausing. Only GKE's auto-upgrade shows it.
  - Expected timeline: each eviction is refused (PDB), GKE waits up to one hour then force-drains;
    measured 64 min per node (op 15:01:40 -> DONE 16:05:56). A 4h window finishes three of five
    nodes; docs: a surge upgrade 'is paused if it runs beyond the window'.
  - On-demand pieces: ends-mid-change needs no event, its endTime is the failure; 'manual still
    runs' under a covering exclusion was shown by break-upgrade.sh upgrading 1.31->1.32 under
    hold-gpu-minor. Never upgrade seeded-b: a control plane cannot be downgraded.
- Verify:
  - Pause: gcloud container operations list --zone $Z --project $P --filter="targetLink~$C AND
    operationType=UPGRADE_NODES" --format='table(name,status,startTime,endTime)': an op started in
    the window; after 07:00Z, open (no endTime) or closed with the pool split.
  - Split pool: kubectl get nodes -l cloud.google.com/gke-nodepool=stall-pool -o
    custom-columns=NAME:.metadata.name,VER:.status.nodeInfo.kubeletVersion = 2 versions; gcloud
    container node-pools describe stall-pool --cluster $C --zone $Z --format=value(version)
  - Automatic, not manual: gcloud logging read "resource.type=gke_nodepool AND
    resource.labels.cluster_name=$C AND protoPayload.metadata.operationType=UPGRADE_NODES" --project
    $P --format='value(timestamp,protoPayload.methodName)': UpdateClusterInternal = auto
  - Ends-mid-change: python3
    agents/platform/skills/fleet-upgrade-verification/scripts/fleet_upgrade_report.py --readiness
    --project $P --at <change start>: 'ends-mid-change blocks auto-upgrade to <target> until <end>';
    at <change end>: 'no exclusion in effect'
- Detect before:
  - Window vs pool: gcloud container clusters describe $C --zone $Z
    --format='json(maintenancePolicy.window)' (recurringWindow.window start/end or
    dailyMaintenanceWindow.duration) vs node count x ~64 min per PDB-stalled node. Unread: readiness
    prints length only.
  - Covering exclusion: readiness field maintenance.blocking_exclusions ('blocks auto-upgrade to
    <target> until <end>', from maintenanceExclusionOptions.scope); read today, graded by case
    upgrades-fleet-readiness-exclusion. SOP 3.8 skips NO_MINOR_UPGRADES.
  - Exclusion end inside the change: maintenancePolicy.window.maintenanceExclusions.<name>.endTime
    between the change's start and end; assert it is outside. Readiness --at gives one instant's
    verdict but knows no change dates, so the comparison is unread.
- Detect after:
  - Op open past the window: gcloud container operations list --zone $Z --project $P
    --filter="targetLink~$C AND status=RUNNING" non-empty after the readiness window's closes_at.
    Unread: the skill's stall rule diffs its own last run, not operations.
  - Mixed versions in one pool: kubectl get nodes -l cloud.google.com/gke-nodepool=<pool> -o
    custom-columns=NAME:.metadata.name,VER:.status.nodeInfo.kubeletVersion returns two values.
    Unread: the skill reads nodePools[].version, one value per pool.
  - Unplanned upgrade after the exclusion lapsed: gcloud logging read "resource.type=gke_cluster AND
    resource.labels.cluster_name=$C AND protoPayload.metadata.operationType=UPGRADE_MASTER"
    --project $P: methodName ...UpdateClusterInternal after endTime. Unread.
- Fix before:
  - Window >= nodes x drain time: gcloud container clusters update $C --zone $Z
    --maintenance-window-start 2000-01-01T00:00:00Z --maintenance-window-end 2000-01-01T08:00:00Z
    --maintenance-window-recurrence FREQ=DAILY
  - Blue-green where the window is tight (docs: 'continues until completion, even if it exceeds the
    maintenance window'): gcloud container node-pools update <pool> --cluster $C --zone $Z
    --enable-blue-green-upgrade
  - Exclusion end outside the change: gcloud container clusters update $C --zone $Z
    --remove-maintenance-exclusion ends-mid-change, then re-add ending after the change; keep a
    covering one only while the hold is wanted.
- Fix after:
  - Finish the pool by hand (bypasses the window): re-run gcloud container clusters upgrade $C
    --node-pool=<pool> --cluster-version <same target> --zone $Z, the docs' resume for a paused
    surge upgrade; or extend the window
  - Or roll back: gcloud container node-pools rollback <pool> --cluster $C --zone $Z (docs: for an
    upgrade 'incomplete due to a maintenance window timing out'). Nothing rolls back a control
    plane; re-add an exclusion

- Cost and time: 5 e2-small ~$2/day (list ~$0.017/h) plus $0.10/h cluster fee; a node's stall ~64
  min; the pause waits on GKE's phased rollout (weeks); exclusion tests: minutes
- Exists today: Covering exclusion: fleet role on main (seeded-b hold-the-minor-lag, case
  upgrades-fleet-readiness-exclusion) and gemma-gpu setup.sh. Window-too-short and
  exclusion-ends-mid-change: nothing; no script, no case, no check.
- Caveats:
  - The window arithmetic assumes GKE's 'up to one hour' PDB wait per node (measured twice on
    gemma-gpu); real pools drain in minutes, so 'too short' there means hundreds of nodes, which no
    test cluster should carry.
  - Whether a paused automatic upgrade keeps its operation RUNNING between windows was not measured;
    the docs say only that the surge upgrade 'is paused'. verify 1 accepts either; the pool's two
    versions are the firm signal.

### 6. A served API version is removed

Home: an out-of-tree cluster. No standing fleet role can carry it: it needs a 1.31 control plane and
a removal only 1.32 makes, and 1.31 leaves EXTENDED on 2026-10-22. The gemma-gpu scripts plant and
break it; the merged deprecated-api-caller stands in for the pre-upgrade audit signal.

- Plant:
  - No GPU needed: gcloud container clusters create <name> --zone us-central1-a --release-channel
    extended --cluster-version <newest 1.31 patch> --num-nodes 1 --machine-type e2-small. Patch:
    get-server-config's EXTENDED validVersions (1.31.14-gke.2759000 today).
  - kubectl create namespace kubeagents-system; kubectl apply -f
    clusters/gemma-gpu/manifests/deprecated-api-caller.yaml (SA, ClusterRole, binding, ConfigMap,
    CronJob legacy-flowcontrol-tuner writing FlowSchema legacy-batch-lane via v1beta3 every 10 min).
  - Or CLUSTER=<name> ./setup.sh (same dir): also the L4 pool, vLLM and PDB for scenarios 1 and 2,
    the first write as Job tuner-first-run, and exclusion hold-gpu-minor (NO_MINOR_UPGRADES, end
    2026-10-21T00:00Z) so GKE does not move the minor before you do.
  - Fleet stand-in, merged (bench/tf/fleet/defects-a.tf, role deprecated-api-caller):
    kubernetes_cron_job_v1.legacy_endpoints_writer in ns seeded-deprecation patches Endpoints
    legacy-endpoints-lane every 10 min; stamped k8s.io/deprecated=true, never breaks.
  - Git-scan path: commit a manifest with apiVersion: flowcontrol.apiserver.k8s.io/v1beta3, kind:
    FlowSchema to a linked GitOps repo, or point --manifests-dir at it; removed_apis.json beside the
    scanner lists that pair as removed_in 1.32 (tested: one table row).
- Break:
  - gcloud container clusters upgrade <name> --master --cluster-version <newest 1.32 patch> --zone
    us-central1-a --quiet (break-upgrade.sh). Blocks until done. Measured: UPGRADE_MASTER 17:55:24
    to 18:04:34; last good write 18:00:01; server on 1.32.13 by 18:01:10.
  - Run the caller once: kubectl -n kubeagents-system create job
    --from=cronjob/legacy-flowcontrol-tuner tuner-after-upgrade-$(date +%s). 18:04:45: BROKEN:
    server v1.32.13 no longer serves flowcontrol/v1beta3 (discovery returned 404). Every later run
    fails.
  - Not reversible: GKE does not downgrade a control plane to a previous minor. Keep a second copy
    (CLUSTER=<name>-2 ./setup.sh) for broken and working side by side. Node pools do not enter into
    it; the API server alone stops serving the version.
- Verify:
  - kubectl get --raw /apis/flowcontrol.apiserver.k8s.io/v1beta3 -> NotFound on 1.32
    (APIResourceList on 1.31); kubectl get --raw
    /apis/flowcontrol.apiserver.k8s.io/v1/flowschemas/legacy-batch-lane -> 200: the object survives,
    only the removed version is gone.
  - kubectl -n kubeagents-system get jobs -l app=legacy-flowcontrol-tuner -o
    custom-columns=NAME:.metadata.name,FAILED:.status.failed -> post-upgrade Jobs show 1; kubectl
    logs -l app=legacy-flowcontrol-tuner --tail=1 --prefix: ok lines, then BROKEN ones.
  - Before: gcloud logging read 'resource.type=k8s_cluster AND resource.labels.cluster_name=<name>
    AND labels."k8s.io/removed-release"="1.32"' --freshness 2d -> one entry per 10-min write,
    methodName io.k8s.apiserver.flowcontrol.v1beta3.flowschemas.patch.
  - gcloud container operations list --zone us-central1-a --filter='targetLink~<name> AND
    operationType=UPGRADE_MASTER' --format='value(status,startTime,endTime)' -> DONE; gcloud
    container clusters describe <name> --format='value(currentMasterVersion)' -> 1.32.x.
- Detect before:
  - gcloud logging read '<cluster filter> AND labels."k8s.io/removed-release"="1.32"' --freshness
    30d; assert empty before targeting 1.32; entries name the caller (callerSuppliedUserAgent).
    logging read is allowlisted, but nothing runs this filter: unread.
  - gcloud recommender insights list --location us-central1-a --insight-type
    google.container.DiagnosisInsight --filter 'insightSubtype:DEPRECATION_K8S_1_32_API AND
    targetResources:<name>'; assert empty. Human-only: not allowlisted. Still absent 2026-09-28.
  - api_deprecation_scan.py --current-version 1.31 --target-version 1.32 --repo <owner/name> or
    --manifests-dir; assert no table row for flowcontrol.apiserver.k8s.io/v1beta3 (exit 0 with
    hits). Skill-read; Helm templates, Helm Secrets, CRD storedVersions: unread.
- Detect after:
  - kubectl -n kubeagents-system get jobs -l app=legacy-flowcontrol-tuner -o
    custom-columns=NAME:.metadata.name,FAILED:.status.failed,START:.status.startTime; a Job started
    after the operation's endTime with FAILED 1 and a BROKEN log line is the break. Unread.
  - kubectl get --raw /apis/flowcontrol.apiserver.k8s.io/v1beta3 returning NotFound on a member
    whose GitOps manifests or Helm release still declare that group version (the scan's row set
    intersected with the served versions). Unread.
  - The audit trail goes quiet: the same gcloud logging read with --freshness 1h returns nothing
    (empty on the upgraded copy 2026-09-28). The failed discovery GET is a read: Data Access logs,
    off by default; only the caller's own log shows it. Unread.
- Fix before:
  - Migrate the caller, not the object: sed
    's#flowcontrol.apiserver.k8s.io/v1beta3#flowcontrol.apiserver.k8s.io/v1#'
    manifests/deprecated-api-caller.yaml | kubectl apply -f -; the audit label stops within 10 min.
  - Git: rewrite apiVersion to flowcontrol.apiserver.k8s.io/v1 via the GitOps suggestion path. Helm:
    a human installs the helm-mapkubeapis plugin, then helm mapkubeapis <release> --namespace <ns>;
    v1beta3 is in its map.
  - Hold the minor: a NO_MINOR_UPGRADES exclusion (setup.sh's hold-gpu-minor) holds only
    auto-upgrade; its end cannot pass 1.31's end of support, 2026-10-22, when GKE upgrades anyway.
    Clear it after 30 silent days.
- Fix after:
  - Object intact through v1: same sed | kubectl apply, then kubectl -n kubeagents-system create job
    --from=cronjob/legacy-flowcontrol-tuner tuner-fixed; its log: ok ... patched FlowSchema
    legacy-batch-lane (HTTP 200).
  - Helm: helm mapkubeapis rewrites the release record (Secret or ConfigMap driver) so helm upgrade
    stops refusing. A CRD whose status.storedVersions lists a dropped version: migrate its objects,
    then patch that field.

- Cost and time: One e2-small zonal cluster plus GKE's EXTENDED per-cluster-hour fee (no L4). Plant
  ~10 min, control-plane upgrade ~10 min (measured 9m10s), verify in 15 min.
- Exists today: Out-of-tree only: gemma-gpu scripts; break measured once (gemma-gpu-upgraded,
  2026-09-24); gemma-gpu still on 1.31 with the live caller. Fleet: deprecated-api-caller merged on
  seeded-a (Endpoints). No bench case yet.
- Caveats:
  - Time-boxed: 1.31 leaves EXTENDED on 2026-10-22 and no later minor removes a served API (the
    migration guide ends at v1.32), so after that date GKE cannot create it; only the Endpoints
    stand-in and the Git scan remain.
  - No DEPRECATION_K8S_1_32_API insight has appeared (none at the 2026-09-27 refresh, 500+ labelled
    writes), and a manual upgrade bypasses GKE's auto-upgrade pause: an absent insight is no clean
    bill; read the audit label.

### 7. A fail-closed webhook whose backend is not up

Home: seeded fleet, in an open pull request. an open pull request (bench/tf/fleet/defects-b.tf)
plants seeded-fail-closed-gate on seeded-b, confined to fleet-labelled ConfigMap CREATE in
seeded-upgrade: detectable, never breakable; applied to no project seen 2026-09-28. The break runs
on a disposable own cluster.

- Plant:
  - Fleet (an open pull request defects-b.tf, tofu apply per project): VWC seeded-fail-closed-gate
    on seeded-b: hook gate.seeded.invalid, failurePolicy Fail, timeoutSeconds 30 (API max), service
    seeded-upgrade/nonexistent-admission-gate; rule core v1 configmaps CREATE only.
  - Fleet scope: namespaceSelector kubernetes.io/metadata.name=seeded-upgrade plus objectSelector
    managed-by=kube-agents-seeded-fleet, so nothing real is ever rejected; the cluster-wide
    dimension is deliberately left to a unit test (see bench-fleet-catalog.md).
  - Own project, P=<proj> Z=us-central1-a: gcloud container clusters create wh-test --zone $Z
    --project $P --release-channel regular --num-nodes 1 --machine-type e2-small. The default pool
    gets GKE's defaults maxSurge 1, maxUnavailable 0.
  - NEXT=$(gcloud container get-server-config --location $Z --project $P --flatten=channels
    --filter=channels.channel=REGULAR --format='value(channels.validVersions[0])'); gcloud container
    clusters upgrade wh-test --master --cluster-version $NEXT --zone $Z -q
  - Once kubectl get pods -A is all Running: kubectl create ns webhook-test; kubectl -n webhook-test
    create service clusterip dead-gate --tcp=443:8443 (selector app=dead-gate, no pods); apply VWC
    dead-gate: gate.dead.invalid, sideEffects None, Fail, timeout 30
- Break:
  - Rule: apiGroups [""], apiVersions [v1], operations [CREATE], resources [pods], scope Namespaced;
    service {webhook-test, dead-gate, /validate}; no caBundle; no namespaceSelector, so kube-system
    matches. Plant check: kubectl run p --image=pause --dry-run=server
  - Fleet: observe only. printf 'kind: ConfigMap\napiVersion: v1\nmetadata: {name: p, namespace:
    seeded-upgrade, labels: {managed-by: kube-agents-seeded-fleet}}\n' | kubectl --context
    gke_${P}_${Z}_seeded-b apply --dry-run=server -f - (persists nothing)
  - Own cluster: gcloud container clusters upgrade wh-test --node-pool default-pool --zone $Z
    --project $P -q --async. Expected (unmeasured): surge node, then cordon+drain (no PDB, so it
    finishes), then delete; all pod CREATEs meanwhile rejected, kube-system too
- Verify:
  - Fleet, derived from apiserver source (fixture applied nowhere): Error from server
    (InternalError): ... failed calling webhook "gate.seeded.invalid": failed to call webhook: Post
    "https://nonexistent-admission-gate.seeded-upgrade.svc:443/validate?timeout=30s"
  - kubectl get events -A --field-selector reason=FailedCreate -o
    custom-columns=NS:.involvedObject.namespace,OBJ:.involvedObject.name,MSG:.message -> Error
    creating: Internal error occurred: failed calling webhook "gate.dead.invalid": ... no endpoints
    available
  - kubectl get ds -n kube-system -o
    custom-columns=NAME:.metadata.name,DESIRED:.status.desiredNumberScheduled,READY:.status.numberReady
    -> READY below DESIRED; kubectl get rs -A -o
    custom-columns=N:.metadata.name,D:.spec.replicas,C:.status.replicas -> C below D
  - gcloud logging read 'resource.type="k8s_cluster" AND resource.labels.cluster_name="wh-test" AND
    protoPayload.status.message:"failed calling webhook"' --project $P --freshness 1h --format
    'value(timestamp,protoPayload.methodName)'; plus operations list
- Detect before:
  - kubectl get validatingwebhookconfigurations,mutatingwebhookconfigurations -o json | jq
    '.items[]|.metadata.name as
    $n|.webhooks[]|select(.failurePolicy=="Fail")|{$n,hook:.name,t:.timeoutSeconds,svc:.clientConfig.service,ns:.namespaceSelector,rules}'
    - unread.
  - Per Fail hook naming a Service: kubectl -n <svc ns> get endpointslice -l
    kubernetes.io/service-name=<svc> -o jsonpath='{.items[_].endpoints[_].conditions.ready}' prints
    a true; timeoutSeconds <=10; namespaceSelector excludes kube-system if rules match pods.
  - gcloud recommender insights list --location $Z --insight-type google.container.DiagnosisInsight
    --filter targetResources:<cluster> --format 'table(insightSubtype,severity)' ->
    K8S_ADMISSION_WEBHOOK_UNAVAILABLE / _UNSAFE. Human-read; cadence unknown
- Detect after:
  - kubectl get events -A --field-selector reason=FailedCreate | grep 'failed calling webhook' ->
    non-empty; the message names the hook and its Service, the source of truth for which webhook is
    wedging the cluster. Unread.
  - kubectl get --raw /metrics | grep
    'apiserver_admission_webhook_rejection_count{.*name="gate.dead.invalid"' -> a series with
    error_type="calling_webhook_error",rejection_code="500" rising during the upgrade (labels
    name,type,operation,error_type,rejection_code)
  - The DaemonSet and ReplicaSet gaps from verify. No Pending pods in this variant: a rejected pod
    object never exists, so kubectl get pods -A --field-selector status.phase=Pending stays empty;
    the catalogue's Pending signal does not fire here. Unread.
- Fix before:
  - Webhook's Git source (not the fleet fixture): namespaceSelector matchExpressions [{key:
    kubernetes.io/metadata.name, operator: NotIn, values: [kube-system]}]; timeoutSeconds 5 (max
    30); Ignore unless a security control
  - Backend: Deployment replicas >=2, PodDisruptionBudget maxUnavailable 1,
    topologySpreadConstraints on kubernetes.io/hostname; kubectl -n <ns> get endpointslice -l
    kubernetes.io/service-name=<svc> lists ready endpoints
  - Serving cert valid for <svc>.<ns>.svc: kubectl run tls --rm -i --restart=Never
    --image=alpine/openssl --command -- openssl s_client -connect <svc>.<ns>.svc:443 </dev/null
    2>/dev/null | openssl x509 -noout -dates
- Fix after:
  - Unwedge: kubectl patch validatingwebhookconfiguration dead-gate --type=json
    -p='[{"op":"replace","path":"/webhooks/0/failurePolicy","value":"Ignore"}]' or delete it.
    Controllers retry with backoff capped at 1000 s.
  - Confirm: kubectl get events -A --field-selector reason=SuccessfulCreate. Once the endpointslice
    shows ready:true, re-apply from Git with the fix_before changes and re-run detect_before. Fleet:
    nothing; fixture stays.

- Cost and time: Fleet: free. Own cluster ~$0.12/h ($0.10 fee unless the free zonal one, plus
  e2-small): create ~8 min, master ~9 (measured), plant 5, pool ~10 unmeasured
- Exists today: Fleet role readiness-failclosed-webhook (seeded-b) in an open pull request,
  applied nowhere 2026-09-28; no eval case reads it; no out-of-tree script; gemma-gpu plants no
  webhook; readiness mode reads PDBs, exclusions, skew
- Caveats:
  - A missing Service or empty endpoints fail fast, so the wedge is immediate; the 30 s timeout
    bites only when a backend accepts connections and hangs. Neither variant is timed; the fleet
    fixture is applied nowhere yet
  - The timeline is mechanism, not measurement. With --enable-dataplane-v2 the anetd DaemonSet pod
    is rejected too and the surge node may never go Ready, stalling before any drain; whether GKE
    reports DONE is unknown

### 8. A default changes in the new minor

Home: an out-of-tree cluster. Version-bound: the 'before' needs a kubelet below 1.33; 1.31 to 1.33
are EXTENDED-only (get-server-config, us-central1-a) and the fleet is REGULAR (main.tf). A gitRepo
Deployment on seeded-a's 1.35 nodes would sit permanently broken, so none is proposed.

- Plant:
  - Cluster (gcloud below: --zone us-central1-a --project haoxuw-gke-dev): gcloud container clusters
    create gitrepo-132 --release-channel extended --cluster-version 1.32.13-gke.2504000 --num-nodes
    1 --machine-type e2-small --quiet; surge defaults 1/0.
  - Or reuse gemma-gpu-upgraded (default-pool 1.31.14) after gcloud container clusters upgrade
    gemma-gpu-upgraded --node-pool default-pool --cluster-version 1.32.13-gke.2427000 --quiet:
    gitRepo still mounts on 1.32; a 1.31 to 1.33 pool jump is undocumented.
  - Deployment legacy-gitrepo-sync, ns kubeagents-system, busybox:1.36, sh -c 'ls /src; sleep
    999999'; volume src: gitRepo {repository: https://github.com/kubernetes/examples, directory:
    '.'} at /src; nodeSelector cloud.google.com/gke-nodepool: default-pool.
  - Before: kubectl -n kubeagents-system get pod -l app=legacy-gitrepo-sync shows Running and its
    log lists the clone. The kubelet runs git on the node (GKE Standard has it, 2024 report); a
    FailedMount 'executable file not found' means no before state.
  - PSA: kubectl create ns psa-rehearsal; kubectl label ns psa-rehearsal
    pod-security.kubernetes.io/warn=restricted pod-security.kubernetes.io/audit=restricted; the same
    Deployment is admitted with 'would violate PodSecurity "restricted:latest"' warnings.
- Break:
  - gcloud container clusters upgrade <cluster> --master --cluster-version 1.33.13-gke.1721000
    --quiet (9 min measured for 1.31 to 1.32; exclusions do not block manual upgrades; the pod keeps
    its volume), then the same with --node-pool default-pool.
  - maxSurge 1 adds a 1.33 node and drains the old; the new pod sits in ContainerCreating,
    FailedMount 'git-repo volume plugin has been disabled; if necessary, it may be re-enabled by
    enabling the feature-gate `GitRepoVolumeDriver`' (kubelet SetUp, v1.33.0).
  - Admission form: kubectl label --overwrite ns psa-rehearsal
    pod-security.kubernetes.io/enforce=restricted, then kubectl -n psa-rehearsal rollout restart
    deploy/legacy-gitrepo-sync. The running pod is untouched; the new pod is refused, the rollout
    stalls.
- Verify:
  - kubectl get events -A --field-selector reason=FailedMount -o
    custom-columns=POD:.involvedObject.name,MSG:.message : expect 'MountVolume.SetUp failed for
    volume "src" : git-repo volume plugin has been disabled'. Text from source; unmeasured on GKE.
  - kubectl get pod -A -l app=legacy-gitrepo-sync -o jsonpath='{.items[0].status.phase}
    {.items[0].status.containerStatuses[0].state.waiting.reason} {.items[0].spec.nodeName}': Pending
    ContainerCreating <node>; that node runs kubelet v1.33.13-gke.1721000.
  - The operation says nothing: gcloud container operations list
    --filter='operationType=UPGRADE_NODES AND targetLink~<cluster>'
    --format='value(status,statusMessage)' reads DONE; no pods.create with
    protoPayload.response.reason Forbidden in the audit log.
  - PSA: kubectl -n psa-rehearsal get events --field-selector reason=FailedCreate -o
    jsonpath='{.items[*].message}' contains 'is forbidden: violates PodSecurity "restricted:latest"'
    (GKE's sample); rollout status deploy/legacy-gitrepo-sync --timeout=60s exits 1.
- Detect before:
  - Live spec scan: kubectl get pods,deploy,sts,ds,jobs,cronjobs -A -o json | jq -r '.items[] |
    select([.. | objects | select(has("gitRepo"))] | length > 0) | .kind+"
    "+.metadata.namespace+"/"+.metadata.name' ; assert empty before any pool moves to 1.33+. Unread.
  - On a 1.33+ control plane kubectl apply --dry-run=server -f <manifest> says
    'spec.template.spec.volumes[0].gitRepo: deprecated in v1.11, and disabled by default in v1.33+'
    (measured on 1.35.7; not on 1.32.13). Unread: the scan reads apiVersion.
  - PSA: kubectl get ns -L pod-security.kubernetes.io/enforce (empty = privileged), read against the
    target notes. SOP 2.11 grades runAsNonRoot/runAsUser/seccomp and skips enforce=restricted
    namespaces; volume types, capabilities and the labels are unread.
- Detect after:
  - kubectl get events -A --field-selector reason=FailedMount -o
    custom-columns=NS:.involvedObject.namespace,POD:.involvedObject.name,MSG:.message | grep
    'git-repo volume plugin has been disabled' ; assert no rows. Unread: readiness reads PDBs,
    exclusions, skew.
  - Catalogue: kubectl get pods -A --field-selector status.phase=Pending -o name | wc -l before the
    pool upgrade and once the operation reads DONE; a rise is the finding. Listed in
    upgrade-readiness-checks.md 'Rollout and verification' as not built: unread.
  - PSA, GKE's query: gcloud logging read 'resource.type="k8s_cluster" AND
    protoPayload.methodName="io.k8s.core.v1.pods.create" AND
    (labels."pod-security.kubernetes.io/audit-violations":"PodSecurity" OR
    protoPayload.response.reason="Forbidden")'. Unread.
- Fix before:
  - Replace the volume as kubectl explain pod.spec.volumes.gitRepo says: an initContainer running
    git clone into an emptyDir, mounted by the main container. Re-run the server dry-run: the
    gitRepo warning is gone.
  - Order the upgrade: control plane first, hold default-pool on 1.32 (one minor behind; GKE allows
    two) until the spec scan and dry-run are clean, then upgrade the pool.
  - PSA: keep warn and audit on the namespace until the pod passes restricted (runAsNonRoot,
    seccompProfile RuntimeDefault, allowPrivilegeEscalation false, drop ALL, allowed volume type),
    then enforce: the catalogue's order.
- Fix after:
  - Apply the fix (initContainer + emptyDir) to deploy/legacy-gitrepo-sync; the new pod mounts and
    runs. No toggle: GKE exposes no kubelet feature gates; 1.36 locks GitRepoVolumeDriver to false
    (kube_features.go).
  - PSA: the rejection names each rule. Fix the pod's securityContext and volumes, or restore
    admission first with kubectl label --overwrite ns psa-rehearsal
    pod-security.kubernetes.io/enforce=baseline, then tighten.

- Cost and time: One e2-small zonal EXTENDED cluster, hours, plus the extended-support fee
  (1.32/1.33 past standard support); create ~10 min, control plane ~9, pool minutes.
- Exists today: Nothing exists: no fleet role on main or in an open pull request; gemma-gpu plants a removed API and
  a drain block, not a flipped default. gemma-gpu-upgraded (cp 1.32.13, default-pool 1.31.14,
  measured) is a ready host.
- Caveats:
  - The break lands on 1.33 nodes: kube_features.go sets GitRepoVolumeDriver Default false at 1.33
    (kubernetes/kubernetes#129923), LockToDefault at 1.36 (kubernetes/kubernetes#136400), the catalogue's '1.36'. The kubelet event text is
    from source, unmeasured on GKE.
  - 1.31 to 1.33 are EXTENDED-only (1.32.13-gke.2504000, 1.33.13-gke.1721000 today); once 1.32
    leaves, no offered minor admits the volume and the 'before' is gone. The PSP to PSA example
    (1.24 to 1.25) is unreproducible.

### 9. A feature is deprecated but still served

Home: seeded fleet, planted today. The deprecated-api-caller role on main (seeded-a, ns
seeded-deprecation) is the fixture: a writer of Endpoints v1, deprecated in 1.33, still served.
externalIPs cannot join the fleet: GKE admission denies the field by default. IPVS: not settable
on GKE.

- Plant:
  - On main, bench/tf/fleet/defects-a.tf: ns seeded-deprecation; CronJob legacy-endpoints-writer
    (*/10 * * * *, backoffLimit 0) merge-patches annotation seeded-last-run onto Endpoints
    legacy-endpoints-lane (192.0.2.10:9) under a headless, selector-less Service.
  - SA legacy-endpoints-writer, Role verbs [patch] on endpoints only, egress-only NetworkPolicy to
    the API server on 443. Apply per project: cd bench/tf/fleet && tofu apply
    -var="project_id=<project>". writer.py refuses masters below 1.33 (BROKEN, exit 1).
  - externalIPs, throwaway cluster only: GKE's DenyServiceExternalIPs admission (default since 1.21)
    rejects it ('Use of external IPs is denied by admission control', seen on gemma-gpu-upgraded
    1.32). Create with --enable-service-externalips, apply the Service.
  - Out-of-tree: any 1.33+ cluster takes the Endpoints trio by kubectl apply as-is. The fleet stays
    externalIPs-denied: enabling it (service_external_ips_config { enabled = true } on
    google_container_cluster) reopens CVE-2020-8554 and undoes a GKE default.
  - kube-proxy IPVS (1.35): not plantable on GKE. kube-proxy is a static pod per node whose args set
    no --proxy-mode (seeded-a: --kubeconfig, --cluster-cidr, sync periods) and there is no
    kube-proxy ConfigMap ('configmaps "kube-proxy" not found'). Nothing to see.
- Break:
  - No break: the entry's After is 'none yet'. The event is the control-plane upgrade to the
    deprecating minor. Endpoints (1.33) is behind every fleet master (seeded-a v1.35.8); externalIPs
    turns on at 1.36, which REGULAR already offers (1.36.4-gke.1247000).
  - The 1.36 edge, throwaway: gcloud container clusters create <c> --zone <z> --release-channel
    regular --enable-service-externalips --num-nodes 1; apply the Service; gcloud container clusters
    upgrade <c> --master --cluster-version 1.36.4-gke.1247000; re-apply.
  - The removal is out of range: the notice's timeline is kube-proxy support disabled at v1.40
    earliest (opt-in remains), disabled completely at v1.43 earliest; Endpoints has no removal.
    Scenario 6's end on gemma-gpu-upgraded: op DONE 18:04:34, BROKEN 18:04:45.
- Verify:
  - kubectl get endpoints legacy-endpoints-lane -n seeded-deprecation prints 'Warning: v1 Endpoints
    is deprecated in v1.33+; use discovery.k8s.io/v1 EndpointSlice' (seen 2026-09-28 on seeded-a,
    v1.35.8); with --warnings-as-errors: exit 1, '1 warning received'.
  - gcloud logging read 'resource.type=k8s_cluster AND resource.labels.cluster_name=seeded-a AND
    labels."k8s.io/deprecated"="true" AND
    protoPayload.authenticationInfo.principalEmail:legacy-endpoints-writer' --freshness 1h:
    endpoints.patch per 10 min (144 in 24 h).
  - No removal, no insight: the same read with labels."k8s.io/removed-release":* over 2d returns
    nothing; gcloud recommender insights list --location <z> --insight-type
    google.container.DiagnosisInsight --filter targetResources:seeded-a has no DEPRECATION_* row.
  - kubectl get jobs -n seeded-deprecation -l app=legacy-endpoints-writer shows Complete 1/1 every
    10 min, none Failed; kubectl get endpoints legacy-endpoints-lane -n seeded-deprecation -o
    jsonpath='{.metadata.annotations.seeded-last-run}' is <10 min old.
- Detect before:
  - Objects: kubectl get endpoints -A --warnings-as-errors --no-headers | wc -l (exit 1 = deprecated
    API in use; 9 rows on seeded-a); kubectl get svc -A -o json | jq
    '[.items[]|select(.spec.externalIPs!=null)]|length' (0 on seeded-a; GKE admission keeps it 0).
  - Callers: the logging read above minus the principal clause, plus the GKE docs' filter
    principalEmail!~("system:serviceaccount:kube-system:"), grouped by principalEmail + methodName
    (seeded-a, 24 h: writer 144 patch, apiserver 2 update). Assert no growth.
  - Not a stand-in: the Recommender (DEPRECATION_* starts once the removal is in the next minor;
    none on seeded-a) and api_deprecation_scan.py (removed_apis.json is as_of 1.32, newest
    removed_in 1.32, nothing for core/v1 Endpoints or externalIPs). Checked today.
- Detect after:
  - Once gcloud container operations list --zone <z> --filter 'operationType=UPGRADE_MASTER AND
    targetLink~<cluster>' shows DONE, rerun both reads. Endpoints trail unchanged; on a 1.36 master
    a Service write with externalIPs now returns a Warning. Unread.
  - The after-signal that matters: a labels."k8s.io/removed-release" entry or a DEPRECATION_K8S_*
    insight appearing, meaning the deprecation became scenario 6. Assert neither is present (true
    for Endpoints and externalIPs on 1.36). Unread; humans read insights.
  - Trend: the per-principal count from before against the same read after; a count that did not
    fall means nobody migrated in the window. This is the 'track the count across runs' the entry
    asks for. Unread; the skill keeps no such record today.
- Fix before:
  - Endpoints: move each caller to discovery.k8s.io/v1 EndpointSlice (the Warning names it), e.g.
    kubectl get endpointslices instead of endpoints. The fleet's writer stays: it is the fixture,
    not a caller to fix.
  - externalIPs on GKE: a type: LoadBalancer Service (GKE assigns the IP) or a Gateway with
    spec.addresses; keep DenyServiceExternalIPs on (the default; org policy
    container.managed.denyServiceExternalIPs holds it).
  - Track: write the per-principal count into the skill's per-run record
    (/opt/data/state/fleet-upgrade-verification/) so the next run reports the delta; that is the
    migration plan the entry's Mitigate-before asks for.
- Fix after:
  - None needed yet (the entry's own line). The same migration applies, now against a date:
    kube-proxy drops externalIPs at v1.40 earliest, everything at v1.43 earliest; Endpoints has no
    removal date.
  - If detect_after shows k8s.io/removed-release or a DEPRECATION_K8S_* insight, it is scenario 6:
    migrate the caller; GKE resumes auto-upgrade after 30 consecutive days without a detected call.

- Cost and time: Fleet: no added cost, already applied per project; verify is five minutes of reads.
  1.36 edge: a throwaway e2-small cluster; a zonal master upgrade took 9 min.
- Exists today: Fleet role deprecated-api-caller on main, applied and yielding in the dev copy
  (seeded-a v1.35.8, 144 stamps in 24 h, no Failed job); no eval case reads it yet. externalIPs:
  nothing; throwaway-only on GKE. IPVS: none.
- Caveats:
  - The label alone proves nothing: kube-system principals (endpoint-controller,
    namespace-controller) and system:apiserver are stamped k8s.io/deprecated=true too. Filter by
    principal + methodName, as the fleet README does.
  - Only writes leave an audit trail (Admin Activity); reads need Data Access logs (Admin Read, Data
    Read), off by default. A tool that only reads Endpoints is invisible there, and the Warning
    header is its only signal.

### 10. Add-on and client skew

Home: an out-of-tree cluster. Blocking skew needs a pool 3 minors behind the target: outside GKE's
two-minor policy, and the fleet cannot hold a specific old minor; seeded-b's pool takes the
master's pin on purpose (main.tf). A derived default-2 pool there would only read 'at ceiling'.

- Plant:
  - No GPU. L='--zone $Z --project $P'. EXTENDED validVersions on 2026-09-28 still list
    1.31.14-gke.2759000: gcloud container clusters create skew-lab $L --release-channel extended
    --cluster-version 1.31.14-gke.2759000 --num-nodes 1 --machine-type e2-small
  - Add setup.sh's window flags to the create: --maintenance-window-start 2000-01-01T03:00:00Z
    --maintenance-window-end 2000-01-01T07:00:00Z --maintenance-window-recurrence FREQ=DAILY. A
    channel rejects autoUpgrade=false; GKE's alternative is an exclusion:
  - gcloud container clusters update skew-lab $L --add-maintenance-exclusion-name hold-nodes
    --add-maintenance-exclusion-scope no_minor_or_node_upgrades --add-maintenance-exclusion-end
    2026-10-21T00:00:00Z (start optional; GKE refuses an end past EOL 2026-10-22)
  - Add-on: helm repo add jetstack https://charts.jetstack.io; helm install cert-manager
    jetstack/cert-manager -n cert-manager --create-namespace --version <a release two minors below the target's support matrix> --set
    crds.enabled=true (chart default false). Matrix (cert-manager.io/docs/releases): 1.29 to 1.33
  - Client: curl -fLo kubectl-old https://dl.k8s.io/release/v1.30.0/bin/$(uname -s | tr A-Z
    a-z)/$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')/kubectl; chmod +x kubectl-old: one
    minor behind 1.31, no warning yet. Baseline: the version commands under verify
- Break:
  - Step 1: gcloud container clusters upgrade skew-lab --master --cluster-version
    1.32.13-gke.2504000 $L --quiet (a 1.32 in validVersions). Manual upgrades bypass exclusions (GKE
    docs); the pool stays on 1.31, 1 behind. gemma-gpu measured ~9 min a minor.
  - Step 2: the same upgrade to 1.33.13-gke.1721000 (GKE allows one control-plane minor per upgrade).
    Pool now 2 behind, GKE's documented ceiling; cert-manager 1.18 is still inside its matrix (to
    1.33); kubectl-old is 3 behind.
  - Step 3: the same upgrade to 1.34.11-gke.1056000, leaving the pool 3 behind. GKE documents the
    two-minor policy, not the enforcement: record gcloud's answer (refusal, a running operation, or
    a forced pool upgrade). If it runs, cert-manager 1.18 is off its matrix.
- Verify:
  - Versions: gcloud container clusters describe skew-lab $L
    --format='value(currentMasterVersion,currentNodeVersion)' -> 1.33.13-gke.1721000
    1.31.14-gke.2759000; kubectl get nodes -o jsonpath='{.items[*].status.nodeInfo.kubeletVersion}'
    -> v1.31.14-gke.2759000
  - Step 3: keep gcloud's exit code and stderr (a synchronous refusal creates no operation); then
    gcloud container operations list $L --filter='targetLink~skew-lab AND
    operationType=UPGRADE_MASTER' --format='table(status,error.message,startTime,endTime)'
  - Client: ./kubectl-old version 2>&1 | grep WARNING -> after step 1: 'WARNING: version difference
    between client (1.30) and server (1.32) exceeds the supported minor version skew of +/-1'
    (v1.30.0 skew_warning.go, on stderr); the exit code does not change
  - Add-on: kubectl -n cert-manager get pods -o jsonpath='{range .items[*]}{.metadata.name}
    {.status.containerStatuses[0].restartCount}{"\n"}{end}'; kubectl -n cert-manager logs
    deploy/cert-manager --since=10m. Expect no crash loop: off-matrix is untested
- Detect before:
  - Read today: fleet_upgrade_report.py --project $P --target-version 1.34.11-gke.1056000
    --readiness --state-dir /tmp/fuv --kubeconfig-dir /tmp/kc ('1.34.x' fails VERSION_RE) ->
    blocked; skew cell '3 minors behind the target control plane; more than 2 blocks...'
  - Vs 1.33.13-gke.1721000: 'default-pool 1.31.14-gke.2759000: at ceiling (2 minors behind the
    target)'. The exclusion alone blocks a minor target; --at 2026-10-21T01:00:00Z isolates skew.
    SOP 3.2 pool-skew: 1 behind minor (autoUpgrade true), 2 major, 3+ critical
  - Unread: add-on image tags (kubectl get deploy -A -o jsonpath='{range
    .items[_]}{.metadata.namespace}/{.metadata.name}
    {.spec.template.spec.containers[_].image}{"\n"}{end}') vs the vendor matrix; the agent image's
    kubectl (apt cloud-sdk, unpinned) vs servers
- Detect after:
  - Read today (re-run the report, or --rollout-in-progress): grade_member grades the lowest pool
    with the master, so status stays 'lagging' and progress reads 'started', not 'completed'; the
    describe command under verify shows the gap
  - Unread: crash loops. kubectl get pods -A with containerStatuses[].restartCount rising in
    cert-manager, events with reason BackOff, logs. The before/after CrashLoopBackOff and Pending
    compare (upgrade-readiness-checks.md) is specified, not shipped
  - Human-read (off the allowlist): gcloud recommender insights list --project $P --location $Z
    --insight-type google.container.DiagnosisInsight --filter
    insightSubtype:CLUSTER_VERSION_SKEW_UNSUPPORTED; latency undocumented, gemma-gpu's PDB insight
    took a day
- Fix before:
  - Pool: gcloud container clusters upgrade skew-lab --node-pool=default-pool --cluster-version
    <currentMasterVersion> $L (SOP 3.2 remediation); in a real rollout move nodes after each master
    minor
  - Exclusion: gcloud container clusters update skew-lab $L --remove-maintenance-exclusion
    hold-nodes; auto-upgrade then keeps the pool in the window. A human does it; SKILL.md says do
    not propose deleting an exclusion
  - Add-on: helm upgrade cert-manager jetstack/cert-manager -n cert-manager --version <the release whose matrix includes the target> before
    the master moves (only release spanning 1.31 to 1.35; 1.20 needs 1.32+; EOL 2026-07-08). Client:
    kubectl within one minor
- Fix after:
  - Pool: the same node-pool upgrade to currentMasterVersion closes the gap in one step (GKE lets
    nodes skip minors, e.g. 1.32 to 1.34); then remove the exclusion so it does not recur
  - Add-on and client: helm upgrade to a release whose matrix includes the running minor (1.20.x for
    1.32 to 1.35); replace the kubectl binary. A rollout restart is not a fix: the versions are what
    changed

- Cost and time: One e2-small plus the EXTENDED fee; ~15 min setup, ~9 min per master minor x3, up
  to a day for the insight; finish before 1.31's EOL 2026-10-22; delete after
- Exists today: Nothing plants it: no fleet role or open pull request (seeded-b's pool is pinned to its
  master; main.tf says why); gemma-gpu starts pool and master equal. Pool skew: upgrade_readiness.py
  evaluate_skew; add-on/client unread
- Caveats:
  - GKE states the two-minor policy, never a refusal: its troubleshooting page treats nodes 'outside
    this supported window' as a state you fix by moving the pool; step 3 is the measurement. After
    2026-10-22 start from 1.32
  - An add-on off its vendor matrix usually keeps running; a deterministic add-on break needs a
    removed API (entries 6 and 7). This recipe verifies the inventory read; the crash-loop half is
    observe-only

### 11. The control plane is unreachable for minutes on a zonal cluster

Home: an out-of-tree cluster. The break is a control-plane upgrade the read-only fleet cannot run on
demand; a throwaway zonal e2-small cluster in your project gives a repeatable one-minor master
upgrade. The fleet is zonal: 'before' holds on any slot; its 03:00 UTC window is observe-only.

- Plant:
  - Fleet: nothing to add. Every google_container_cluster in bench/tf/fleet/main.tf has location =
    var.zone (variables.tf: 'Zonal on purpose'), so 'before' already holds on seeded-a/b/c. No new
    role: it sits on all three slots, a background row, not a defect.
  - Out-of-tree: `gcloud container get-server-config --location us-central1-a --project $P
--format=json`; NEXT = REGULAR `defaultVersion`; VER = newest REGULAR `validVersions` entry one
    minor below it (setup.sh line 22's python, K8S_MINOR = that minor).
  - `gcloud container clusters create zonal-cp-test --zone us-central1-a --project $P
--release-channel regular --cluster-version "$VER" --num-nodes 1 --machine-type e2-small
--disk-size 32 --quiet` (setup.sh line 24's shape; `--zone` makes it zonal).
  - A caller with no retry, started first: `CTX=gke_${P}_us-central1-a_zonal-cp-test; while :; do
    printf '%s ' $(date -u +%T); kubectl --context $CTX --request-timeout=5s get --raw /readyz
    > /dev/null 2>&1 && echo up || echo DOWN; sleep 2; done | tee probe.log`
  - Optional, for the GitOps symptom: register the cluster in a hub Argo CD with the credential-free
    cluster Secret in docs/site/src/content/docs/deploy/gitops-argocd.md, plus one Application. An
    in-cluster CronJob is no use as caller: its controller is down too.
- Break:
  - `gcloud container clusters upgrade zonal-cp-test --master --cluster-version "$NEXT" --zone
us-central1-a --project $P --quiet` (synchronous; break-upgrade.sh line 16). GKE moves a control
    plane one minor at a time, which is why VER sits one minor under NEXT.
  - Measured once (gemma-gpu-upgraded, 2026-09-24, 1.31 to 1.32; README): CronJob's last 1.31
    success 18:00:01, server on 1.32.13 by 18:01:10, operation DONE 18:04:34, about nine minutes in
    all; the 60 s probe never failed. GKE documents only 'a few minutes'.
  - Observe-only on the fleet: GKE auto-upgrades a master in the 03:00 UTC daily_maintenance_window
    (main.tf; seeded-b's exclusion still lets patches roll) on nights it has one. Run the 2 s probe
    with `--kubeconfig $BENCH_FLEET_KUBECONFIG_DIR/<role>.kubeconfig`.
- Verify:
  - probe.log: either a contiguous run of DOWN lines (kubectl's own error, or a /readyz body other
    than `ok`) bracketed by the operation, or none at all, as the 60 s probe saw. DOWN lines x 2 s
    is the gap: the number this entry currently lacks.
  - `gcloud container operations list --zone us-central1-a --project $P
--filter='operationType=UPGRADE_MASTER AND targetLink~clusters/zonal-cp-test$'
--format='table(name,status,startTime,endTime)'`: one DONE row whose startTime/endTime bracket
    the DOWN lines.
  - `gcloud container clusters describe zonal-cp-test --zone us-central1-a --project $P
--format='value(currentMasterVersion,status)'` prints `$NEXT RUNNING`; `kubectl --context $CTX
get --raw /version` shows the new gitVersion (watch-break.py's `server` field).
  - If a hub Argo CD manages it: `kubectl -n argocd get application -o
custom-columns=NAME:.metadata.name,SYNC:.status.sync.status,COND:.status.conditions[*].type`
    shows Unknown or ComparisonError during the gap and Synced with no condition after its next
    poll.
- Detect before:
  - `gcloud container clusters describe C --location L --format='value(location,locations)'`;
    assert: `location` a zone = one control-plane replica, exposed; a region = not. Unread:
    fleet_upgrade_report.py and fleet_drift.py key on location, never zone-vs-region.
  - `--format='value(currentMasterVersion,maintenancePolicy.window.dailyMaintenanceWindow.startTime)'`
    vs the `get-server-config` channel default; a master below it is eligible in a window GKE picks.
    Read: readiness (window, lag), orchestrator (window).
  - Whether callers retry is not readable from the cluster (per the entry): report the exposure; the
    operator names the callers (CI, GitOps hub, the agent's kubectl). Only related field read today:
    notificationConfig.pubsub.enabled (patch orchestrator SOP 3.10).
- Detect after:
  - Client side: probe.log DOWN lines, or the failing caller's own error (kubectl connection
    refused, i/o timeout, HTTP 5xx); assert the timestamps fall inside the UPGRADE_MASTER
    operation's start/end. Unread by any kube-agents component.
  - Server side: the operations row above, or `gcloud logging read 'resource.type="gke_cluster" AND
protoPayload.metadata.operationType=~"(UPDATE_CLUSTER|UPGRADE_MASTER)" AND
resource.labels.cluster_name="C"' --project P --freshness 2d`; one brackets them. Unread.
  - GitOps: Argo CD `.status.sync.status` Unknown or a ComparisonError condition on hub-managed
    Applications; in-cluster CronJobs show a `.status.lastScheduleTime` gap (controller down too); a
    missed run starts late unless startingDeadlineSeconds passed. Unread.
- Fix before:
  - Location type is immutable: build a regional cluster for what automation depends on (`gcloud
container clusters create ... --region us-central1` or Terraform `location = "<region>"`). Not
    the fleet: zonal on purpose.
  - Bounded, timed calls in callers that cannot wait: `kubectl --request-timeout=15s` under a
    process timeout (watch-break.py lines 17-24); an upgrade script blocks on `gcloud container
operations wait <OP> --zone L`.
  - Window when callers are idle: `gcloud container clusters update C --location L
--maintenance-window-start 2000-01-01T03:00:00Z --maintenance-window-end 2000-01-01T07:00:00Z
--maintenance-window-recurrence FREQ=DAILY`.
- Fix after:
  - Wait; there is nothing to repair. `gcloud container operations wait <OP> --zone L --project P`,
    then `kubectl get --raw /readyz` returns `ok`: GKE's zonal control plane comes back on its own
    once the operation is DONE.
  - GitOps resyncs on its own; to hurry it, `kubectl -n argocd annotate application <app>
argocd.argoproj.io/refresh=hard --overwrite`. Re-run a job the gap skipped: `kubectl create job
--from=cronjob/<name> <name>-rerun`.

- Cost and time: Out-of-tree: one e2-small zonal cluster (GKE fee + one node); create not measured;
  a master minor step ~9 min (README's 1.31->1.32 run). Fleet: free.
- Exists today: No fleet role, no open pull request, nothing reads location type. All fleet slots and
  gemma-gpu are zonal; break-upgrade.sh + watch-break.py ran one master upgrade on 2026-09-24, no
  gap seen at 60 s.
- Caveats:
  - The one measured zonal control-plane upgrade (gemma-gpu-upgraded, 2026-09-24, 1.31 to 1.32)
    showed no API gap at one-minute resolution; GKE's docs say only 'a few minutes' inaccessible.
    The 2 s probe bounds the real gap.
  - The Logging filter is the form in GKE's troubleshooting doc
    (kubernetes-engine/docs/troubleshooting/upgrades), not run here; Argo CD reads assume the site
    doc's hub; run the gcloud operations/logging reads by hand.

### 12. A node label or taint is removed

Home: simulation only. No readable node lacks the beta labels (seeded-a on 1.35.8, read 2026-09-28);
1.36.4/1.37.0 are offered but unread and their changelogs name no label removal. Trigger
simulated: a kubectl-set node label the pool upgrade's node rebuild discards (GKE docs).

- Plant:
  - Subject: P=haoxuw-gke-dev Z=us-central1-a C=gemma-gpu-upgraded; K="kubectl --context
    gke_${P}_${Z}_${C}". default-pool: one e2-small on 1.31.14 under a 1.32.13 master, maxSurge 1,
    config.labels empty, no PDB. Any 1-node CPU pool below its master does.
  - Out-of-band label (never in the pool's config.labels): N=$($K get nodes -l
    cloud.google.com/gke-nodepool=default-pool -o jsonpath='{.items[0].metadata.name}'); $K label
    node $N tier=legacy-cache. Stands in for a label a new node image stops setting.
  - $K create ns upg-12; $K -n upg-12 create deploy label-pinned --image=registry.k8s.io/pause:3.9;
    $K -n upg-12 patch deploy label-pinned -p
    '{"spec":{"template":{"spec":{"nodeSelector":{"tier":"legacy-cache"}}}}}' (no requests, so it
    fits the CPU-full e2-small).
  - Baseline: $K -n upg-12 rollout status deploy/label-pinned succeeds; $K -n upg-12 get pod -o wide
    shows Running on $N; $K get node $N -o json | jq -r '.metadata.labels|keys[]' >
    /tmp/keys-before.txt (the key set the rebuild is measured against).
  - Fleet can hold only the before-state: a seeded-a kubernetes_deployment_v1 shaped like
    checkout_gateway (defects-a.tf) with node_selector = { "beta.kubernetes.io/arch" = "amd64" }; it
    schedules today and the API server warns on apply. Not a role yet.
- Break:
  - gcloud container clusters upgrade $C --node-pool=default-pool --cluster-version=$(gcloud
    container clusters describe $C --zone $Z --project $P --format='value(currentMasterVersion)')
    --zone $Z --project $P --quiet (1.31.14 to 1.32.13; blocks until DONE).
  - Documented surge order (maxSurge 1): new node provisioned and Ready, old node cordoned, drained
    (no PDB, so the eviction succeeds at once), then deleted. The replacement pod is Pending from
    the eviction on. Durations for this pool are unmeasured.
  - GKE's own statement of the mechanism (docs, Manually upgrade a cluster): 'When GKE upgrades a
    node pool, either manually or automatically, GKE removes any labels you added to individual
    nodes using kubectl'; any other change that recreates nodes does too.
- Verify:
  - $K -n upg-12 get pod -o wide: Pending, NODE <none>. $K -n upg-12 get events --field-selector
    reason=FailedScheduling: message contains 'node(s) didn't match Pod's node affinity/selector'
    (scheduler ErrReasonPod; the entry quotes the pre-1.21 wording).
  - Label gone, selector intact: $K get nodes -L tier shows one default-pool node, new name, empty
    TIER, created after the operation's startTime; diff /tmp/keys-before.txt <($K get node <new> -o
    json | jq -r '.metadata.labels|keys[]') prints only '< tier'.
  - gcloud container operations list --zone $Z --project $P --filter='operationType=UPGRADE_NODES
    AND targetLink~'$C --format='table(name,status,startTime,endTime)' reads DONE while the pod is
    Pending: 'complete' means every node recreated, not the workload back.
  - Proof the label was never pool-declared: gcloud container node-pools describe default-pool
    --cluster $C --zone $Z --project $P --format='value(config.labels)' prints nothing before and
    after (gpu-pool prints workload=gemma), so GKE had nothing to re-apply.
- Detect before:
  - Selector keys in use: $K get deploy,sts,ds,job,cronjob -A -o json | jq -r '[.. | objects |
    (.nodeSelector? // {} | keys[]), (.nodeSelectorTerms? // [] | .[].matchExpressions[]? |
    select(.operator|IN("In","Exists")) | .key)] | unique[]'. Unread today.
  - Hand-set keys: any key above on a Node ($K get nodes -o json | jq -r
    '[.items[].metadata.labels|keys[]]|unique[]'), in no pool's config.labels (node-pools describe,
    value(config.labels)) and under no kubernetes.io/, cloud.google.com/ or gke.io/ prefix. Unread.
  - Deprecated keys: kubectl apply --dry-run=server prints 'nodeSelector[beta.kubernetes.io/arch]:
    deprecated since v1.14; use "kubernetes.io/arch" instead' (seen 2026-09-28 on 1.32.13). Warn
    only: GKE's konnectivity-agent selects on beta.kubernetes.io/os. Unread.
- Detect after:
  - $K get events -A --field-selector reason=FailedScheduling: no event newer than the operation's
    startTime carries 'didn't match Pod's node affinity/selector' (filter on message: two kube-dns
    pods are already Pending, Insufficient cpu). No scheduled reader.
  - Label diff: keys in /tmp/keys-before.txt absent from the new node are what the rebuild dropped;
    intersect with detect_before's selector keys to name the stranded workloads. Unread:
    upgrade-readiness-checks.md lists 'a diff after the upgrade' as an addition.
  - Rollup: $K get deploy -A -o
    custom-columns='NS:.metadata.namespace,NAME:.metadata.name,READY:.status.readyReplicas,WANT:.spec.replicas',
    READY below WANT after the operation's endTime. GKE marks the operation DONE regardless; no
    Recommender insight exists.
- Fix before:
  - Pool-declare the label so rebuilt nodes carry it: gcloud container node-pools update
    default-pool --cluster $C --zone $Z --project $P --node-labels=tier=legacy-cache (in place;
    overwrites every user label, list all).
  - Or select on a key GKE or the pool owns: cloud.google.com/gke-nodepool=default-pool,
    kubernetes.io/arch, topology.kubernetes.io/zone. Change it in Git, not with kubectl, so the next
    reconcile does not undo it.
  - Replace beta.kubernetes.io/arch|os|instance-type and
    failure-domain.beta.kubernetes.io/zone|region with the GA keys the apply warning names, while
    nodes still carry both (they do on 1.35.8).
- Fix after:
  - Stopgap, seconds: $K label node <new-node> tier=legacy-cache; the pod schedules. Lost again on
    the next rebuild, so follow with the node-pools update --node-labels from fix_before.
  - Or patch the selector: $K -n upg-12 patch deploy label-pinned -p
    '{"spec":{"template":{"spec":{"nodeSelector":{"cloud.google.com/gke-nodepool":"default-pool"}}}}}';
    the new ReplicaSet schedules at once.

- Cost and time: gemma-gpu-upgraded exists: plant ~3 min, verify ~2 min; the e2-small pool upgrade
  is unmeasured (the GPU node was rebuilt ~4 min after its drain ended); cents.
- Exists today: Nothing in tree. No fleet role; an open pull request's readiness-pinned-workload
  pins pinned-batch-runner to a tainted pool, not a vanishing label. gemma-gpu's scripts do not
  plant it; gemma-gpu-upgraded's default pool serves.
- Caveats:
  - Catalogue trigger unverified and not creatable: 1.35.8 nodes carry all five beta labels (kubelet
    stopped setting os/arch near 1.19; the node controller mirrors them); 1.36/1.37 unread. Recipe
    drops a hand-set label.
  - Taints: removing one blocks nothing; only a taint a new image adds would, and none is known. No
    PDB, so the break kills the pod once and rehearses entry 12 alone, not entry 1. kube-dns's two
    Pending pods pre-exist.

### 13. The container runtime changes

Home: an out-of-tree cluster. Needs a pool on 1.32 (containerd 1.7) moving to 1.33 (2.0): GKE's page
fixes those minors, so not the fleet, whose REGULAR clusters sit on 1.34/1.35 (default 1.35.8) and
already run containerd 2; a role there could only hold the wreckage, not the before.

- Plant:
  - `gcloud container clusters create ctrd2 --project haoxuw-gke-dev --zone us-central1-a
--release-channel extended --cluster-version 1.33.13-gke.1721000 --node-version
1.32.13-gke.2504000 --num-nodes 1 --machine-type e2-small` (the Sep 2026 Extended patches).
  - Hold (same zone): `gcloud container clusters update ctrd2 --add-maintenance-exclusion-name hold
--add-maintenance-exclusion-start $(date -u +%FT%TZ) --add-maintenance-exclusion-end
2026-12-31T00:00:00Z --add-maintenance-exclusion-scope no_minor_upgrades`
  - DaemonSet cri-v1alpha2-agent (ns runtime-legacy, root, socket hostPath): init curlimages/curl
    untars crictl-v1.22.0-linux-amd64.tar.gz (v1alpha2-only) to an emptyDir; busybox loops `crictl
-r unix:///run/containerd/containerd.sock version || exit 1`.
  - DaemonSet schema1-image, same ns: gcr.io/google-containers/startup-script:v1, command `sleep
2147483647`. gcr.io served it as manifest.v1+prettyjws, schemaVersion 1, on 2026-09-28 (curl);
    GKE's containerd-2 page names it. Pulls on 1.7, not on 2.0.
  - Not planted: containerd 1.x config overrides. GKE's path is `--containerd-config-from-file`
    (registryHosts, privateRegistryAccessConfig, writableCgroups) and it rewrites config.toml on
    every node; a privileged DaemonSet editing it is unsupported: observe-only.
- Break:
  - By hand; exclusions hold only automatic upgrades: `gcloud container clusters upgrade ctrd2
--node-pool default-pool --cluster-version 1.33.13-gke.1721000 --zone us-central1-a --quiet
--async`. Default surge 1/0 creates the 1.33 node first.
  - Expected, not measured: the new node is Ready on containerd 2.0 within minutes; both DaemonSet
    pods land on it; schema1-image goes ImagePullBackOff on its first pull, cri-v1alpha2-agent
    CrashLoopBackOff on its first call; the 1.32 node is drained and deleted.
  - UPGRADE_NODES reads DONE once the node is replaced (expect 5-10 min) with both pods failing: as
    on gemma-gpu, the operation grades nodes, not workloads. Auto-upgrade would have paused (GKE
    holds 1.33 until 14 days without detection); a manual one does not.
- Verify:
  - Before: `kubectl get nodes -o
custom-columns=N:.metadata.name,R:.status.nodeInfo.containerRuntimeVersion` shows
    containerd://1.7.x; `kubectl -n runtime-legacy logs ds/cri-v1alpha2-agent` prints
    `RuntimeApiVersion:  v1alpha2`; schema1-image is Running.
  - After: the node reads containerd://2.0.x; `kubectl -n runtime-legacy describe pod -l
app=schema1-image` Events: `Failed to get converter for ...: Pulling Schema 1 images have been
deprecated and disabled by default since containerd v2.0`; ImagePullBackOff.
  - After: `kubectl -n runtime-legacy logs -l app=cri-v1alpha2-agent --previous` ends in `code =
Unimplemented desc = unknown service runtime.v1alpha2.RuntimeService` (2.0 registers no alpha
    service; expected wording, not measured); CrashLoopBackOff.
  - `gcloud container operations list --zone us-central1-a --filter='operationType=UPGRADE_NODES AND
targetLink~ctrd2' --format='value(status,startTime,endTime)'` reads DONE while both pods fail.
    One node, no PDB: no stall or ERROR pool state expected.
- Detect before:
  - Socket clients: GKE's `kubectl get pods -A -o json | jq` over spec.volumes[].hostPath.path in
    its 13-path list (/, /run, /var/run, .../containerd.sock), system namespaces excluded; here it
    prints runtime-legacy/cri-v1alpha2-agent-*; empty is clean. Unread.
  - Node: `kubectl debug node/<n> -it --image=busybox -- chroot /host ctr deprecations list` (1.7
    records cri-api-v1alpha2 and pull-schema-1-image), or GKE's k8s-node-tools
    cri-v1alpha2-api-deprecation-reporter DaemonSet. Human-run; `debug` is no agent verb.
  - `gcloud logging read 'jsonPayload.SYSLOG_IDENTIFIER="containerd" "conversion from schema 1
images is deprecated"' --freshness 1d` (GKE's filter; 1.7 logs that line per pull; allowlisted,
    no skill runs it). Recommender DEPRECATION_CONTAINERD_*: human only.
- Detect after:
  - `kubectl get pods -A | grep -E 'ImagePullBackOff|CrashLoopBackOff'`, then `kubectl describe pod`
    Events and `kubectl logs --previous`; or GKE's `log_id("events") jsonPayload.message=~"Failed to
get converter"`. Read on request; nothing ties them to the node.
  - Correlate with the node: every failing pod sits on a node whose
    `.status.nodeInfo.containerRuntimeVersion` is containerd://2.0.x while any surviving 1.7 node
    (in a larger pool) is clean; assert the failing set equals the upgraded-node set. Unread.
  - `gcloud container operations list --zone us-central1-a --filter='operationType=UPGRADE_NODES'`
    reads DONE and the pool reads 1.33: fleet-upgrade-verification's rollout tracking sees that
    version move and marks the member `completed`; it reads no pod state.
- Fix before:
  - Move the socket client to CRI v1: swap the tarball to crictl-v1.26.0-linux-amd64.tar.gz (v1.24.0
    first spoke v1, with v1alpha2 fallback) and confirm `RuntimeApiVersion:  v1`; vendor agents:
    their containerd-2 build.
  - Schema 1 to 2: `kubectl -n runtime-legacy set image ds/schema1-image
'*=gcr.io/google-containers/startup-script:v2'`; elsewhere a containerd pull (converts) and
    push; then `crane manifest <ref> | jq .schemaVersion` = 2.
  - Keep the no_minor_upgrades exclusion until both reads are clean: GKE's pause needs 14
    detection-free days and its page says to add one anyway. Do not wait on the insight.
- Fix after:
  - Same two edits under pressure: `set image` to :v2 and the crictl tarball bump. DaemonSet pods
    re-pull and restart on their own within a minute or two; no node action is needed.
  - Downgrade the pool in place while 1.32 is offered: `gcloud container clusters upgrade ctrd2
--node-pool default-pool --cluster-version 1.32.13-gke.2504000 --zone us-central1-a`; rollback
    fits only an unfinished op.

- Cost and time: One e2-small zonal EXTENDED cluster (extended-support fee applies); ~15 min to
  build, 5-10 min to break, under an hour total; a day or more only for the insight
- Exists today: Nothing exists: no fleet role on main or an open pull request; gemma-gpu's 1.31 to 1.32
  path keeps containerd 1.7; the one nearby reader, security-patch orchestrator 3.9
  stale-image-type, passes COS_CONTAINERD and stays silent.
- Caveats:
  - Timings and the create-time version pair are untested by a run of this recipe; if create rejects
    `--node-version` below `--cluster-version`, build both on 1.32.13 and upgrade `--master` first,
    as break-upgrade.sh does.
  - cri-v1alpha2-agent must run as root to open the socket and needs egress to github.com for the
    tarball (mirror it to Artifact Registry otherwise); keep runtime-legacy free of a restricted
    pod-security label.

### 14. cgroup v2 under a runtime that cannot read it

Home: an out-of-tree cluster. Fleet is REGULAR (1.34-1.36 offered, default 1.35); an explicit
CGROUP_MODE_V1 pool on seeded-b at 1.34 would die when 1.35 removes v1, so no standing fixture.
Use a fresh 1.32 EXTENDED e2-small cluster in haoxuw-gke-dev; gemma-gpu's pools already read V2.

- Plant:
  - P=haoxuw-gke-dev Z=us-central1-a C=cgroup-test. gcloud container clusters create $C --project $P
    --zone $Z --release-channel extended --cluster-version 1.32.13-gke.2504000 --num-nodes 1
    --machine-type e2-small --quiet. Not gemma-gpu: its 1.31 fixture.
  - printf 'linuxConfig:\n cgroupMode: CGROUP_MODE_V1\n' > cgroup-v1.yaml; gcloud container
    node-pools create legacy-jvm-pool --cluster $C --zone $Z --project $P --machine-type e2-small
    --num-nodes 1 --system-config-from-file cgroup-v1.yaml --quiet
  - Fill.java: add new byte[1<<23] to an ArrayList until OutOfMemoryError, print
    "max="+Runtime.getRuntime().maxMemory(), Thread.sleep(Long.MAX_VALUE). kubectl create ns
    cgroup-test; kubectl -n cgroup-test create configmap fill-src --from-file=Fill.java
  - Deployment legacy-jvm: image eclipse-temurin:11.0.15_10-jdk (below the page's 11.0.16 floor),
    command [java, /src/Fill.java], fill-src at /src, limits.memory 256Mi, nodeSelector
    cloud.google.com/gke-nodepool: legacy-jvm-pool. fixed-jvm: same, 11.0.16_8-jdk.
  - gcloud container node-pools describe legacy-jvm-pool --cluster $C --zone $Z --project $P
    --format='value(config.effectiveCgroupMode,config.linuxNodeConfig.cgroupMode)' ->
    EFFECTIVE_CGROUP_MODE_V1 CGROUP_MODE_V1; both pods Running, each log max= near 126 MiB.
- Break:
  - Sim (GKE's migration path): printf 'linuxConfig:\n cgroupMode: CGROUP_MODE_V2\n' >
    cgroup-v2.yaml; gcloud container node-pools update legacy-jvm-pool --cluster $C --zone $Z
    --project $P --system-config-from-file cgroup-v2.yaml; surge recreation at once.
  - Real: gcloud container clusters upgrade $C --master --cluster-version <1.33 patch> --zone $Z
    --project $P, then 1.34; pool to 1.33 (GKE auto-upgrades pools >2 minors behind; it must still
    read V1); master 1.35; --node-pool=legacy-jvm-pool to 1.35 --async.
  - Expected: node replaced in 5-10 min, pods reschedule; legacy-jvm exits 137 OOMKilled within a
    minute, then CrashLoopBackOff; fixed-jvm stays Running at 256Mi; nothing in Git changed. Docker,
    cgroup v2 host: 11.0.15 killed in <45 s, 11.0.16 stable at 154 MiB.
- Verify:
  - Pool: the plant describe prints EFFECTIVE_CGROUP_MODE_V2; kubectl get nodes -l
    cloud.google.com/gke-nodepool=legacy-jvm-pool shows a new node; gcloud container operations list
    --zone $Z --filter='targetLink~legacy-jvm-pool' reads UPGRADE_NODES DONE.
  - Node (human only: the agent role has pods get/list/watch and pods/log, no exec or create):
    kubectl debug node/<node> -it --image=ubuntu -- stat -fc %T /host/sys/fs/cgroup prints cgroup2fs
    (v1: tmpfs); busybox stat prints UNKNOWN. Delete the debugger pod.
  - kubectl -n cgroup-test get pod -l app=legacy-jvm -o
    jsonpath='{.items[_].status.containerStatuses[_].lastState.terminated}' shows reason OOMKilled,
    exitCode 137, restarts rising; app=fixed-jvm Running, 0 restarts; containerStatuses[*].imageID
    unchanged.
  - Kernel record: kubectl get events -A --field-selector reason=OOMKilling --sort-by=.lastTimestamp
    lists node-problem-detector's node-scoped 'Memory cgroup out of memory: Killed process N (java)'
    on the new node only; it names the process, not the pod.
- Detect before:
  - gcloud container node-pools list --cluster <c> --zone <z>
    --format='table(name,config.effectiveCgroupMode)'; flag EFFECTIVE_CGROUP_MODE_V1 for a 1.33+
    target (1.35+ if pinned). container.viewer covers it; fleet_upgrade_report.py reads
    version/status. Unread.
  - V1-node images: kubectl get pods -A --field-selector spec.nodeName=<node> -o
    jsonpath='{.items[_].spec.containers[_].image}' vs the cgroup page floors (JDK 8u372, 11.0.16,
    15; Node.js 20.3.0; automaxprocs 1.5.1). audit_report.py reads no images. Unread.
  - Dry run on any V2 pool (human only): kubectl exec <pod of the same image and limit> -- java
    -XX:+PrintFlagsFinal -version | grep MaxHeapSize. Measured at 256m: 11.0.15 reports 2078277632
    (a quarter of the host), 11.0.16 132120576. Unread.
- Detect after:
  - kubectl get pods -A -o
    custom-columns='N:.spec.nodeName,P:.metadata.name,L:.status.containerStatuses[*].lastState.terminated.reason'
    | grep OOMKilled; hits on a pool just gone V2, same imageID. gke-workload-troubleshooting reads
    exit 137, not the flip. Unread.
  - Flip: gcloud container operations list --zone <z> --filter='operationType=UPGRADE_NODES'
    --format='table(targetLink,status,startTime,endTime)' with the describe: V1 before, V2 after,
    OOMKilled pods first seen between startTime and endTime, same image. Unread.
  - kubectl -n <ns> describe pod <pod> shows Last State Terminated, Reason OOMKilled, Exit Code 137;
    while it runs, kubectl exec <pod> -- java -XX:+PrintFlagsFinal -version | grep MaxHeapSize shows
    a heap far above the limit (human only: no pods/exec). Unread.
- Fix before:
  - Move to a runtime the cgroup page names: kubectl -n <ns> set image deploy/<d>
    <c>=eclipse-temurin:11.0.16_8-jdk (or 8u372+, 15+; Node.js 20.3.0+; automaxprocs 1.5.1+), then
    rerun the MaxHeapSize read on a V2 pool.
  - Or set the heap explicitly so the runtime's guess stops mattering: kubectl -n <ns> set env
    deploy/<d> JAVA_TOOL_OPTIONS=-Xmx192m (three quarters of 256Mi); for Go, GOMEMLIMIT. The
    Kubernetes page names no .NET version.
  - Buy time below 1.35 only: pin the pool with cgroup-v1.yaml via gcloud container node-pools
    update <pool> --system-config-from-file cgroup-v1.yaml (surge recreation); GKE calls the opt-out
    temporary and 1.35 removes v1.
- Fix after:
  - The same workload fixes (set image to a v2-aware runtime, or set env JAVA_TOOL_OPTIONS=-Xmx...);
    kubectl set resources deploy/<d> --limits=memory=<n> helps only above a quarter of node RAM plus
    overhead.
  - Roll the pool back to v1 only while it is below 1.35: gcloud container node-pools update <pool>
    --system-config-from-file cgroup-v1.yaml (surge recreation, 5-10 min). At 1.35 GKE removes v1;
    only workload fixes remain.

- Cost and time: Two e2-small nodes plus EXTENDED fee. Cluster+pool ~10 min, pods ~2 min; mode-flip
  sim 5-10 min; real walk 3 x ~10 min plus two pool upgrades; kill in <1 min.
- Exists today: Nothing exists: no fleet role, open pull request or gemma-gpu script. Every gemma-gpu and
  gemma-gpu-upgraded pool reads EFFECTIVE_CGROUP_MODE_V2 today; no kube-agents script reads
  effectiveCgroupMode or images.
- Caveats:
  - GKE's page says explicit v1 is a temporary opt-out at 1.33+ and that 1.35 removes v1, but not
    whether a 1.35 pool upgrade migrates or refuses a pinned pool. Observe effectiveCgroupMode and
    the operation's status there.
  - Heap figures were measured under Docker on an 8 GB arm64 cgroup v2 VM, not on GKE; on a 2048 MB
    e2-small the mis-sized heap is ~512 MiB, still past 256Mi. Creating a CGROUP_MODE_V1 pool at
    1.32 was not exercised.

### 15. The OOM killer starts killing the whole container

Home: simulation only. GKE refuses CGROUP_MODE_V1 on clusters created at 1.26+ and offers nothing
older, so cgroup v1 under a kubelet >= 1.28 cannot be built. Simulation: a cheap pool on
gemma-gpu-upgraded (1.32.13, dev project) with the singleProcessOomKill opt-out on, then off.

- Plant:
  - P=haoxuw-gke-dev; Z=us-central1-a; F="--cluster gemma-gpu-upgraded --zone $Z --project $P". echo
    '{kubeletConfig: {singleProcessOomKill: true}}' >sp.yaml; gcloud container node-pools create
    oomg-pool $F --num-nodes 1 --system-config-from-file sp.yaml
  - gcloud container node-pools describe oomg-pool $F
    --format='value(version,config.effectiveCgroupMode,config.kubeletConfig.singleProcessOomKill)'
    -> 1.32.13-gke.2427000 (floor 1.32.4-gke.1132000) EFFECTIVE_CGROUP_MODE_V2 True; e2-medium
    default.
  - Deployment oomg/mpw (supervisor forking an overrunning worker): nodeSelector
    cloud.google.com/gke-nodepool: oomg-pool, busybox:1.36, memory 32Mi/64Mi, command sh -c 'while
    true; do echo worker start; tail /dev/zero; echo "worker exit $?"; sleep 5; done'
  - Baseline (kernel kills only the largest process): K='kubectl -n oomg'; $K logs deploy/mpw prints
    'worker exit 137' every few seconds while $K get pod -o
    jsonpath='{.items[_].status.containerStatuses[_].restartCount}' stays 0. Seen in Docker,
    oom.group 0.
  - In-container proof: $K exec deploy/mpw -- sh -c 'stat -fc %T /sys/fs/cgroup; cat
    /sys/fs/cgroup/memory.oom.group /sys/fs/cgroup/memory.events' -> cgroup2fs, 0, oom_kill N with
    oom_group_kill 0. Containers on both gemma clusters read 1 today (2026-09-28).
- Break:
  - echo '{kubeletConfig: {singleProcessOomKill: false}}' >grp.yaml; gcloud container node-pools
    update oomg-pool $F --system-config-from-file grp.yaml. GKE recreates the node by surge,
    ignoring maintenance policy (node-system-config doc); the pod moves.
  - Timeline (seen in Docker, untimed on GKE): pod Running at T; tail hits 64Mi in seconds; the
    kernel kills sh and tail together (memory.oom.group=1); exit 137, OOMKilled, restartCount 1
    within a minute, then CrashLoopBackOff, back-off up to 5 min.
  - Real triggers cannot be run: crossing 1.28 (EXTENDED starts at 1.31) and the 1.33 migration of a
    v1 pool (v1 needs a cluster created before 1.26). Observe only: a pool at
    EFFECTIVE_CGROUP_MODE_V1 before gcloud container clusters upgrade --node-pool to 1.33.
- Verify:
  - $K get pod -o
    custom-columns='R:.status.containerStatuses[_].restartCount,L:.status.containerStatuses[_].lastState.terminated.reason,E:.status.containerStatuses[*].lastState.terminated.exitCode'
    -> N OOMKilled 137 with N>=1 (was 0 <none> <none>).
  - $K exec deploy/mpw -- sh -c 'stat -fc %T /sys/fs/cgroup; cat /sys/fs/cgroup/memory.oom.group' ->
    cgroup2fs and 1; $K logs deploy/mpw --previous ends at 'worker start' with no 'worker exit'
    line: the supervisor died with its worker (as in Docker).
  - kubectl get events -A --field-selector reason=OOMKilling: kernel-monitor emits one per kernel
    'Killed process N (comm)' line, so (sh) now appears beside (tail); before the flip only (tail).
    $K get events --field-selector reason=BackOff shows the crashloop.
  - gcloud container node-pools describe oomg-pool $F
    --format='value(config.kubeletConfig.singleProcessOomKill)' -> False; gcloud container
    operations list --zone $Z --project $P --filter='targetLink~oomg-pool' shows DONE; the node is
    new (creationTimestamp).
- Detect before:
  - gcloud container node-pools list $F
    --format='table(name,version,config.effectiveCgroupMode,config.kubeletConfig.singleProcessOomKill)':
    flag pools at kubelet >= 1.28 reading V2 (or V1 with target >= 1.33) and no opt-out. Unread;
    readiness reads version only.
  - Containers with more than one process: kubectl exec <pod> -c <ctr> -- sh -c 'ls -d /proc/[0-9]*
    | wc -l' > 1; with no shell, the same via kubectl debug -it <pod> --image=busybox:1.36
    --target=<ctr>. Unread; the obtainability audit reads pins and probes.
  - Kills Kubernetes does not see (GKE OOM-events doc): gcloud logging read
    'resource.type="k8s_node" AND jsonPayload.MESSAGE:"TaskOOM event"', or OOMKilling events, on
    containers at restartCount 0: after the flip that kill takes the container. Unread.
- Detect after:
  - kubectl get pods -A -o
    custom-columns='P:.metadata.name,L:.status.containerStatuses[*].lastState.terminated.reason' |
    grep OOMKilled where restarts were 0 before. The event watcher (filter.go) and
    gke-workload-troubleshooting read it and blame the limit.
  - In the pod cat /sys/fs/cgroup/memory.oom.group -> 1 on a node that postdates the operation
    (kubectl get node <node> -o jsonpath='{.status.nodeInfo.kubeletVersion}
    {.metadata.creationTimestamp}') and the pool's cgroup mode or opt-out changed since. Unread.
  - OOMKilling events for every process of one container; or kubectl debug node/<node>
    --profile=sysadmin --image=busybox:1.36 -- chroot /host dmesg | grep oom.group -> 'Tasks in ...
    are going to be killed due to memory.oom.group set' (mm/memcontrol.c). Unread.
- Fix before:
  - Opt out a pool at 1.32.4-gke.1132000/1.33.0-gke.1748000+ (nodes recreated): echo
    '{kubeletConfig: {singleProcessOomKill: true}}' >sp.yaml; gcloud container node-pools update
    POOL $F --system-config-from-file sp.yaml
  - Size the limit for the whole container, not one worker: raise resources.limits.memory in the
    Git-declared manifest to the sum of the workers' peaks, or split each worker into its own
    container or pod.
  - To buy time only, and only on a cluster created before 1.26: pin linuxConfig.cgroupMode:
    CGROUP_MODE_V1 and hold the target below 1.33 with a NO_MINOR_UPGRADES exclusion, as seeded-b
    does; entry 14 says v1 ends at 1.35.
- Fix after:
  - Same opt-out on the affected pool (singleProcessOomKill: true, nodes recreated) if its version
    allows; else raise the limit now: kubectl set resources -n <ns> deploy/<name>
    --limits=memory=<sum of peaks>, then in Git.
  - Name the worker that used to die from kubectl logs --previous and the pre-upgrade
    TaskOOM/OOMKilling records, then split it out or size the limit from its peak; a return to v1
    exists only on pre-1.26 clusters below 1.35.

- Cost and time: One e2-medium on gemma-gpu-upgraded (about USD 0.03/h plus disk); pool create and
  config flip a few minutes each, untimed; break visible in a minute; no GPU.
- Exists today: Nothing exists: no fleet role (seeded-a/c 1.35.8, seeded-b 1.34.11, all past the
  1.33 migration), no open pull request, no gemma-gpu script; the
  four gemma pools read V2, opt-out unset (2026-09-28).
- Caveats:
  - Simulated trigger: an opt-out flip on a v2 pool reproduces memory.oom.group 0 to 1, not a
    cgroup-mode migration or a 1.27 to 1.28 upgrade; mechanics verified in Docker and by reads on
    GKE, the flip itself not yet run.
  - GKE's doc table spells the key singleProcessOOMKill; gcloud's parser (util.py) and the API take
    singleProcessOomKill, which the recipe uses. An after-only fleet role would look like
    crashloop-workload; none is proposed.

### 16. The network dataplane changes

Home: simulation only. Not creatable on demand: a version upgrade changes neither
networkConfig.datapathProvider nor dnsConfig.clusterDns, and the fleet carries no dataplane
change. Closest safe simulation: two throwaway clusters in the user's project, one per dataplane.

- Plant:
  - What an upgrade moves is the enforcer's version (kube-proxy/Calico on legacy, anetd/Cilium on
    V2, kube-dns), not the provider; whether behaviour changes is in the target's known-issue notes.
    So the plant is a rehearsal harness, not a defect.
  - `gcloud container clusters create net-v1 --zone us-central1-a --cluster-version <cur>
--num-nodes 1 --machine-type e2-standard-2 --workload-pool=<p>.svc.id.goog
--enable-network-policy`; net-v2: last flag becomes `--enable-dataplane-v2
--cluster-dns=clouddns`.
  - `kubectl create ns kubeagents-system denied`; apply gemma's manifests/networkpolicy.yaml
    (vllm-policy, selects app=gemma-server) on both, and in `denied` the fleet's default-deny
    (defects-a.tf default_deny: empty podSelector, Ingress+Egress, no rules).
  - Probe (same ns): `kubectl run probe --image=busybox:1.36 -l app=gemma-server -- httpd -f -p
8000`; rows: `nslookup -timeout=2 kubernetes.default`; `wget -T3 --spider` to `--header
'Metadata-Flavor: Google' http://169.254.169.254/` and to `https://google.com`.
  - Clients: unlabelled `c1` in the same ns (`wget -T3 --spider http://<probe-ip>:8000` ok), `c2` in
    `denied` (drops); baseline.txt: ALLOW/DENY/TIMEOUT per row. V2: `kubectl patch networklogging
default --type merge -p '{"spec":{"cluster":{"deny":{"log":true}}}}'`
- Break:
  - Per cluster: `gcloud container clusters upgrade net-v1 --master --cluster-version <target>
--zone us-central1-a --quiet` (one minor per control-plane step), then the same with
    `--node-pool default-pool` in place of `--master`. Repeat for net-v2.
  - Gemma's timings: control plane 4.5 to 9 minutes; a one-node pool rebuild about 3 minutes with no
    PDB in the way. The enforcers are DaemonSets, so a behaviour change lands per node after the
    pool step: re-run the matrix then, not after the control-plane step.
  - To move DNS on demand, on net-v1 before its pool step: `gcloud container clusters update net-v1
--cluster-dns=clouddns --zone us-central1-a`; docs: Pods keep kube-dns until the pool is
    upgraded to a new version, then use 169.254.169.254:53. Else no diff.
- Verify:
  - Matrix diff: `diff baseline.txt after.txt`. Expected: empty on a benign upgrade. A row moving
    ALLOW to DENY or TIMEOUT is the break; the row names the flow (DNS, metadata, external 443,
    pod-to-pod). A `denied` row moving to ALLOW is enforcement lost.
  - Drops, V2 only: `gcloud logging read 'resource.type="k8s_node" AND
resource.labels.cluster_name="net-v2" AND logName="projects/<p>/logs/policy-action" AND
jsonPayload.disposition="deny"' --freshness 1h --format
'value(timestamp,jsonPayload.policies)'`.
  - DNS: probe `nslookup -timeout=2 kubernetes.default` answers; `grep nameserver /etc/resolv.conf`
    in it reads the kube-dns VIP, or 169.254.169.254 once Cloud DNS is in effect; `kubectl -n
kube-system get deploy kube-dns` READY vs desired (gemma-gpu: 1/2 today).
  - Operation status is not the verdict: `gcloud container operations list --zone us-central1-a
--filter 'targetLink~net-v1' --format 'table(operationType,status,endTime)'` reads DONE either
    way (gemma's pool read DONE while in ERROR). Legacy emits no policy logs.
- Detect before:
  - `gcloud container clusters describe <c> --zone <z>
--format='value(networkConfig.datapathProvider,networkConfig.dnsConfig.clusterDns,networkPolicy.enabled,addonsConfig.networkPolicyConfig.disabled)'`;
    empty = legacy, kube-dns. fleet_drift.py reads 1, 3, 4.
  - Count vs enforcer: `kubectl get networkpolicy -A --no-headers | wc -l` with the read above;
    count above 0 and neither ADVANCED_DATAPATH nor networkPolicy.enabled=true means nothing
    enforces them (gemma-gpu: 1 policy, legacy). SOP 2.6 counts; join unread.
  - Known issues for the target:
    docs.cloud.google.com/kubernetes-engine/docs/troubleshooting/known-issues (today: Calico
    pod-scheduling regression 1.32 to 1.35, a 1.35 Dataplane V2 entry), read by a human at run time
    (upgrade-readiness-checks.md:178). Unread.
- Detect after:
  - Re-run the probe matrix after the pool step and diff against baseline.txt; assertion: no row
    changed. Unread by any component; the readiness mode of fleet-upgrade-verification runs no
    in-cluster probe.
  - V2: the deny query from verify, asserting zero entries with jsonPayload.connection.dest_port 53
    or 988 or the probe as jsonPayload.src.pod_name; jsonPayload.policies names the dropping policy.
    Unread: no component reads policy-action logs.
  - DNS timeouts: probe `nslookup -timeout=2` failing; on kube-dns clusters `kubectl -n kube-system
get deploy kube-dns` READY below desired. kube-dns keeps running after a Cloud DNS switch, so
    there resolv.conf and the probe row are the signal. Unread.
- Fix before:
  - Rehearse: build the copy with production's datapathProvider and clusterDns at the target
    (`--cluster-version <target>`), apply the same policies from Git, run the matrix, and schedule
    production only on an empty diff.
  - Make policy explicit: swap a lone kube-dns podSelector or literal VIP for networkpolicy.yaml's
    dual peers; if unenforced: `clusters update --update-addons=NetworkPolicy=ENABLED`, then
    `--enable-network-policy`.
  - Commit baseline.txt beside the policies so the post-upgrade diff has a reference; a matrix
    nobody recorded before the upgrade cannot show what moved.
- Fix after:
  - Downgrade the pool while offered (not below control plane minus two minors): `gcloud container
clusters upgrade <c> --node-pool default-pool --cluster-version <prev> --zone <z>`. The control
    plane cannot go back a minor.
  - Fix the moved row: add the peer the new path needs (DNS VIP ipBlock; 169.254.169.254/32:53 for
    Cloud DNS; 169.254.169.252/32:988), re-run; on V2 jsonPayload.policies names it.
    `--cluster-dns=default` reverts Cloud DNS.

- Cost and time: Two single-node e2-standard-2 zonal clusters (Calico's documented minimum rules out
  e2-small), deleted after the session; 15 to 25 min per cluster per round.
- Exists today: Nothing planted: fleet Terraform sets no datapath, DNS or policy fields; gemma-gpu,
  gemma-gpu-upgraded and seeded-a/b/c read legacy, kube-dns, addon disabled (2026-09-28); gemma-gpu
  has 1 unenforced policy; no open pull request.
- Caveats:
  - Catalogue's 'Read today: nothing' is off: fleet_drift.py reads datapathProvider,
    networkPolicy.enabled and the addon flag as drift facets. seeded-a/b/c read legacy plus
    disabled, so their default-deny enforces nothing.
  - A DNS row DENY before any upgrade is the manifest, not the dataplane: vllm-policy names
    10.96.0.10/32 or a non-private VIP; read `kubectl -n kube-system get svc kube-dns -o
jsonpath='{.spec.clusterIP}'`, fix the peer.

### 17. A node networking agent fails on the new image

Home: simulation only. Not creatable on demand: needs a GKE node image that breaks netd, kube-proxy
or node-local-dns; no fleet role can hold a pool-rebuild event. Closest safe simulation on
gemma-gpu-upgraded: a stand-in per-node agent whose kubectl-set label a rebuild drops.

- Plant:
  - P=haoxuw-gke-dev Z=us-central1-a C=gemma-gpu-upgraded (master 1.32.13, EXTENDED, legacy
    dataplane); K="kubectl --context gke_${P}_${Z}_${C}"; every gcloud below takes --zone $Z
    --project $P. V=newest offered 1.31 patch from get-server-config (setup.sh idiom).
  - gcloud container node-pools create canary-pool --cluster $C --node-version "$V" --num-nodes 1
    --machine-type e2-small --max-surge-upgrade 1 --max-unavailable-upgrade 0
    --node-labels=role=canary --quiet (1.31.14-gke.2704000 is not offered). Recreate to repeat.
  - Stand-in agent: ns sim-netagent, DaemonSet sim-node-agent, hostNetwork true,
    registry.k8s.io/pause:3.10, tolerations [{operator: Exists}], nodeSelector
    netagent.sim/enabled=true; then $K label nodes --all netagent.sim/enabled=true (kubectl, not
    --node-labels).
  - Service VIP probe: DaemonSet svc-probe (label app=svc-probe, busybox:1.36, pod network),
    readinessProbe exec sh -c 'nslookup kubernetes.default.svc.cluster.local && nc -z -w 2
    kubernetes.default.svc.cluster.local 443', periodSeconds 5; both cross a ClusterIP.
  - Baseline: $K get ds -n sim-netagent (DESIRED == node count == READY for both); $K get ds -n
    kube-system netd -o jsonpath='{.status.desiredNumberScheduled}/{.status.numberReady}' (2/2
    today; selector cloud.google.com/gke-netd-ready); kube-proxy at 0 restarts.
- Break:
  - gcloud container clusters upgrade $C --node-pool=canary-pool --cluster-version=$(gcloud
    container clusters describe $C --format='value(currentMasterVersion)') --quiet --async: 1.31 to
    1.32.13, the node is recreated.
  - Event without a version move: gcloud container clusters upgrade $C --image-type
    UBUNTU_CONTAINERD --node-pool canary-pool --quiet. GKE recreates the nodes at once under the
    pool's upgrade strategy, regardless of maintenance policy.
  - Documented surge order (maxSurge 1 / maxUnavailable 0): new node provisioned and Ready without
    the kubectl label; old node cordoned, drained (DaemonSet pods not evicted), deleted;
    UPGRADE_NODES DONE. sim-node-agent DESIRED holds, then drops by one.
- Verify:
  - $K get ds -n sim-netagent sim-node-agent -o
    jsonpath='{.status.desiredNumberScheduled}/{.status.numberReady}' reads one below the node
    count; $K get pods -n sim-netagent -o wide lists no sim-node-agent pod on the new node (new
    suffix, newer than the op).
  - $K get nodes -L netagent.sim/enabled: the new node's column is empty. Ready stays True and
    NetworkUnavailable False (reason RouteCreated) everywhere, netd DESIRED == READY, kube-proxy
    restarts 0, every svc-probe pod Ready: GKE's real agents are untouched.
  - gcloud container operations list --filter='operationType=UPGRADE_NODES AND
    targetLink~canary-pool' --format='table(name,status,statusMessage,startTime,endTime)' reads DONE
    while the stand-in agent is missing from the new node: the status does not cover it.
  - Audit: gcloud logging read 'resource.type="k8s_cluster" AND resource.labels.cluster_name="'$C'"
    AND protoPayload.methodName=("io.k8s.core.v1.nodes.create" OR "io.k8s.core.v1.nodes.delete")'
    --freshness 2h: the create's request.metadata.labels: GKE keys only.
- Detect before:
  - DaemonSet selector keys: $K get ds -A -o
    custom-columns='NAME:.metadata.name,SEL:.spec.template.spec.nodeSelector,AFF:.spec.template.spec.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[_].matchExpressions[_].key'
  - Assert each key is pool-declared (gcloud container node-pools describe POOL --cluster $C
    --format='value(config.labels)') or GKE/kubelet-owned (kubernetes.io/, node.kubernetes.io/,
    cloud.google.com/, iam.gke.io/, sandbox.gke.io/). Only sim-node-agent fails.
  - Target notes: gcloud container get-server-config --format='yaml(channels)'; read GKE release
    notes and known issues for netd, kube-proxy, node-local-dns. Pools: node-pools list --cluster $C
    --format='table(name,config.imageType,upgradeSettings)'. Human read.
- Detect after:
  - Node conditions: $K get nodes -o jsonpath='{range .items[*]}{.metadata.name}
    {.status.conditions[?(@.type=="Ready")].status}
    {.status.conditions[?(@.type=="NetworkUnavailable")].status}{"\n"}{end}' reads True False on
    every node (reason RouteCreated). Unread.
  - GKE's agents: $K get ds -n kube-system netd DESIRED == READY (anetd on Dataplane V2;
    node-local-dns absent here); $K get pods -n kube-system -l component=kube-proxy -o
    custom-columns='NODE:.spec.nodeName,R:.status.containerStatuses[0].restartCount'. Unread.
  - $K get pods -n sim-netagent -l app=svc-probe -o wide: one Ready pod per node; a Not Ready one
    names the node whose Service VIPs fail; sim-node-agent DESIRED == node count. The 'diff after
    the upgrade' upgrade-readiness-checks.md asks for; unread today.
- Fix before:
  - Pool-declare the label: gcloud container node-pools update canary-pool --cluster $C
    --node-labels=role=canary,netagent.sim/enabled=true (in place; replaces all user labels;
    rebuilds keep it). Or select on a GKE-set key.
  - Surge everywhere: gcloud container node-pools update POOL --cluster $C --max-surge-upgrade 1
    --max-unavailable-upgrade 0; upgrade canary-pool first and hold the rest until svc-probe, netd
    and kube-proxy are green.
  - Known issue names an agent: hold automatic node upgrades: gcloud container clusters update $C
    --add-maintenance-exclusion-name/-start/-end/-scope no_minor_or_node_upgrades (setup.sh form).
    Manual upgrades still run.
- Fix after:
  - Canceled, failed or incomplete upgrade: gcloud container node-pools rollback canary-pool
    --cluster $C. Once DONE: downgrade via clusters upgrade --node-pool --cluster-version to an
    offered version within two minors.
  - Restore the selector: node-pools update --node-labels as above (kubectl label is a stopgap the
    next rebuild drops); $K -n sim-netagent rollout status ds/sim-node-agent. GKE agent: $K -n
    kube-system logs <pod> --previous.

- Cost and time: Plant ~10 min; one e2-small, cents per hour; a one-node rebuild is minutes (no
  canary figure; gemma-gpu's in-place rebuild took 3 min, 16:02:00 to 16:05:11).
- Exists today: Nothing exists today: no fleet role on main or an open pull request; gemma-gpu scripts
  cover entries 1, 2 and 6 only; the catalogue reads 'Read today: nothing', no Recommender insight.
  Scenario 12 plans the same event.
- Caveats:
  - Simulation covers the selector mechanism only: netd, kube-proxy and Service VIPs stay healthy;
    the routing outage itself is observe-only, in a real event. Same trigger as scenario 12,
    different agent and signals.
  - GKE removes kubectl-applied node labels on any rebuild (documented). node-pools rollback covers
    canceled, failed or incomplete upgrades only; once DONE the only path is a downgrade to a
    version the channel still offers.

### 18. GPU driver mismatch

Home: an out-of-tree cluster. Needs an L4 pool, which the seeded fleet cannot hold. Lives beside
gemma-gpu in gke-fleet-iac (haoxuw-gke-dev, us-central1-a): a copy of the cluster, a second L4
pool on gpu-driver-version=default, a CUDA-13 probe pod. setup.sh plants only a latest pool.

- Plant:
  - Baseline measured 2026-09-28 on gemma-gpu: gpu-pool 1.31.14-gke.2704000,
    gpuDriverInstallationConfig.gpuDriverVersion=LATEST; installer log 'Installing GPU driver
    version 580.173.02'; nvidia-smi header 'CUDA Version: 13.0'; vLLM torch 2.10.0+cu129.
  - GKE how-to gpus table: rows 1.31, 1.32, 1.33 each list R535 (default) and R570/R575/R580, so
    latest is R580 on all three. NVIDIA release notes: CUDA 13.x runs on drivers >=580, 12.x
    > =525.60.13, 11.x >=450.80.02; R535 stops at CUDA 12.2 (12.3 needs 545.23.06).
  - On the copy: gcloud container node-pools create gpu-pool-default --cluster gemma-gpu-driver
    --zone us-central1-a --machine-type g2-standard-4 --accelerator
    type=nvidia-l4,gpu-driver-version=default --max-surge-upgrade 0 --max-unavailable-upgrade 1
  - Deployment cuda13-probe: image pytorch/pytorch:2.10.0-cuda13.0-cudnn9-runtime, command sh -c
    'python3 -c "import torch; assert torch.cuda.is_available()" && exec sleep infinity',
    nvidia.com/gpu: 1, nodeSelector cloud.google.com/gke-nodepool: gpu-pool-default.
  - Leave the serving gpu-pool alone: deployment.yaml selects
    cloud.google.com/gke-gpu-driver-version: latest, so vLLM stays on R580. Sleep tail: a container
    exiting 0 under a Deployment also reaches CrashLoopBackOff, so Running vs CrashLoopBackOff is
    the tell.
- Break:
  - Control plane first (GKE versioning: nodes can't run newer than it): break-upgrade.sh step 1 to
    1.32.13-gke.2504000 (EXTENDED, ~9 min measured), then gcloud container clusters upgrade
    gemma-gpu-driver --node-pool=gpu-pool-default --cluster-version <it> --async
  - Expected: the 1.32 row keeps default=R535, so the rebuilt node reinstalls R535 and cuda13-probe
    crashes again once rescheduled; the upgrade re-applies the mismatch, not causes it. In-place
    rebuild under maxSurge 0 measured ~3 min (16:02 to 16:05).
  - The catalogue's direction (an upgrade lowering the driver below the images' floor) cannot be
    forced: default R535 and highest R580 on every row 1.31-1.33. The mismatch is the default pin
    plus a CUDA-13 image; the upgrade only shows the pin survives it.
- Verify:
  - Driver on the node: kubectl -n kube-system logs <nvidia-gpu-device-plugin-*-cos pod on it> -c
    nvidia-driver-installer | grep 'Installing GPU driver version' -> expect 535.x on
    gpu-pool-default (580.173.02 measured on the latest pool; init log stays readable).
  - From any pod on the node: /usr/local/nvidia/bin/nvidia-smi --query-gpu=driver_version
    --format=csv,noheader (not on PATH; GKE mounts it there, checked live). Header 'CUDA Version:
    12.2' on R535 is the ceiling; torch.version.cuda in the probe is 13.0.
  - kubectl get pod -l app=cuda13-probe -> CrashLoopBackOff; kubectl logs --previous -> UserWarning
    'CUDA initialization: The NVIDIA driver on your system is too old (found version ...)' then
    AssertionError (torch maps cudaErrorInsufficientDriver 35 to 0 GPUs).
  - The upgrade reports success regardless: gcloud container operations list --zone us-central1-a
    --filter='targetLink~clusters/gemma-gpu-driver/nodePools/gpu-pool-default AND
    operationType=UPGRADE_NODES' --format='table(name,status,statusMessage)' -> DONE.
- Detect before:
  - Pool pin: gcloud container node-pools describe <pool> --cluster <c> --zone <z>
    --format='value(version,config.accelerators[0].gpuDriverInstallationConfig.gpuDriverVersion)'
    (allowlisted in command_policy.py; LATEST live). Map it via the TARGET's row. Unread.
  - Image floor: per pod requesting nvidia.com/gpu, its CUDA major: kubectl exec <pod> -- sh -c 'ls
    -d /usr/local/cuda-*' (12.9 in the vLLM image) or python3 -c 'import
    torch;print(torch.version.cuda)'. Assert driver branch >= floor: 13.x >=580, 12.x >=525. Unread
  - Nodes: kubectl get nodes -l cloud.google.com/gke-accelerator -L
    cloud.google.com/gke-gpu-driver-version (values default|latest) plus each GPU workload's
    nodeSelector. Assert no CUDA-13 workload lands on a default node. Unread; gke-upgrades SKILL.md
    only warns.
- Detect after:
  - GPU pods: kubectl get pods -A -o wide | grep -E 'CrashLoopBackOff|Pending', then kubectl
    describe / logs --previous: 'Insufficient nvidia.com/gpu' (Pending form, no driver) or the
    driver-too-old / cudaErrorInsufficientDriver line (crash form). Unread as cause.
  - Device plugin: kubectl get nodes -l cloud.google.com/gke-accelerator -o
    custom-columns='NODE:.metadata.name,GPU:.status.allocatable.nvidia\.com/gpu' -> 1 when the
    driver installed (checked live), <none> when it failed; the installer log says why. Unread.
  - Operation status: gcloud container operations list --filter='operationType=UPGRADE_NODES' shows
    DONE and the pool RUNNING while the workload is down; the operation carries no driver or
    workload signal (live: statusMessage empty on success). Unread.
- Fix before:
  - Move the pool to the branch the images need before upgrading: gcloud container node-pools update
    gpu-pool-default --cluster gemma-gpu-driver --zone us-central1-a --accelerator
    type=nvidia-l4,gpu-driver-version=latest
  - Pin CUDA-13 workloads with nodeSelector cloud.google.com/gke-gpu-driver-version: latest (as
    gemma's deployment.yaml does) so they cannot land on a default node; or rebuild the image on
    CUDA 12.x, which R535 runs to 12.2.
  - Canary: upgrade one GPU pool first and run the workload's own image on it; troubleshooting.md
    section 9's nvidia/samples:vectoradd-cuda11.6.0 passes on any driver >=450.80.02, so it cannot
    test a CUDA-13 floor.
- Fix after:
  - Same node-pools update to gpu-driver-version=latest; the driver installs at node creation, so
    nodes must be rebuilt (not on GKE's documented recreation list: watch operations list and node
    AGE). Pods recover on restart.
  - If even latest is below the floor (not on 1.31-1.33): move to a GKE version whose table row
    lists the branch, or rebuild on a lower CUDA. gpu-driver-version=disabled +
    daemonset-preloaded.yaml gives the same COS driver.

- Cost and time: One extra g2-standard-4+L4 per test hour, plus the copy cluster; us-central1-a L4
  stock is thin (stockout 2026-09-24). Break at first schedule; rebuild ~3 min.
- Exists today: Nothing plants it. Fleet: GPU excluded. gemma-gpu (setup.sh) has one L4 pool on
  gpu-driver-version=latest, no mismatch. Repo: prose only (gke-upgrades SKILL.md 148-153,
  troubleshooting.md section 9, checklists.md 27).
- Caveats:
  - The simulation shows the pairing (default pin + CUDA-13 image), not an upgrade lowering a
    driver; GKE's table keeps R535 default and R580 highest across 1.31-1.33, so no reachable
    upgrade produces that transition.
  - R535 on a default pool and the crash text come from the GKE table, NVIDIA notes and torch
    source, not observed here. pytorch/pytorch is ubuntu:24.04-based, no cuda-compat libs, so no
    forward-compat path masks the floor.

### 19. In-tree volumes lose their CSI path

Home: an out-of-tree cluster. The 1.22 crossing cannot be built: the oldest version offered in
us-central1-a is 1.31.14 (EXTENDED only; validMasterVersions bottoms at 1.34.9), so every new
cluster runs CSI migration; the staged event is the add-on toggle. A seeded-a role is feasible.

- Plant:
  - Cluster, any offered version (migration is on everywhere): gcloud container clusters create
    intree-pd --project $P --zone $Z --release-channel regular --num-nodes 2 --machine-type e2-small
    --disk-size 32 --quiet. Two nodes so a re-attach can hit another node.
  - Disk: gcloud compute disks create intree-pd-1 --project $P --zone $Z --size 10GB --type
    pd-standard. PV intree-pd-1: capacity 10Gi, ReadWriteOnce, reclaimPolicy Retain,
    storageClassName legacy-gce-pd, gcePersistentDisk {pdName: intree-pd-1, fsType: ext4}.
  - StorageClass legacy-gce-pd, provisioner kubernetes.io/gce-pd (in-tree code removed in k8s 1.28;
    the name works only via CSI translation). PVC intree-pd in ns intree: storageClassName
    legacy-gce-pd, volumeName intree-pd-1, 10Gi. Deployment intree-pd, pause:3.9.
  - Prove the fixture: kubectl -n intree get pod -l app=intree-pd -> Running; kubectl get
    volumeattachment -> ATTACHER pd.csi.storage.gke.io, PV intree-pd-1, ATTACHED true; gcloud
    compute disks describe intree-pd-1 --zone $Z --format='value(users)' names the node.
  - Fleet shape (seeded-a only; the kubernetes provider reaches no other slot): addons_config {
    gce_persistent_disk_csi_driver_config { enabled = false } } on seeded_a, a google_compute_disk,
    kubernetes_persistent_volume_v1 (gce_persistent_disk.pd_name), PVC, pod.
- Break:
  - Event: gcloud container clusters update intree-pd --zone $Z
    --update-addons=GcePersistentDiskCsiDriver=DISABLED. GKE PD CSI page: with it off 'the
    gcePersistentDisk volume type also stops working'; running pods 'do not terminate', new pods
    'fail to start'.
  - Nothing breaks until a re-attach. Stage it: kubectl cordon <node of the pod>; kubectl -n intree
    delete pod -l app=intree-pd. The replacement lands on the other node and needs an attach no
    controller performs. First event within ~2 min (csiTimeout 2m).
  - Upgrade proper: gcloud container clusters upgrade intree-pd --zone $Z --node-pool default-pool
    --cluster-version <pool's current version> re-creates nodes (drain + replace). Expect the drain
    to wait on the pod that cannot unmount, up to GKE's documented hour.
- Verify:
  - kubectl -n intree get pod -l app=intree-pd: new pod ContainerCreating (phase Pending); old pod
    expected Terminating (kubelet TearDownAt needs the node plugin). describe new pod Events:
    FailedAttachVolume 'Multi-Attach error ... already used by pod(s) <old>'.
  - Other texts (k8s 1.35 src, unobserved): first-ever attach (fleet shape) -> 'timed out waiting
    for external-attacher of pd.csi.storage.gke.io CSI driver to attach volume'; same-node relaunch
    -> FailedMount 'not found in the list of registered CSI drivers'.
  - kubectl get volumeattachment: cordon path keeps the old row (ATTACHED true, NODE old), adds
    none; fleet shape: one row ATTACHED false, never flips. gcloud compute disks describe
    intree-pd-1 --zone $Z --format='value(users)' names the old node till its VM goes.
  - kubectl -n kube-system get ds pdcsi-node -> NotFound; kubectl get csidriver
    pd.csi.storage.gke.io (record if it survives); add-on read (detect_before 1) -> not True. gcloud
    container operations list --zone $Z --filter='targetLink~intree-pd' -> DONE.
- Detect before:
  - gcloud container clusters describe $C --zone $Z
    --format='value(addonsConfig.gcePersistentDiskCsiDriverConfig.enabled)'; assert True on every
    Standard cluster (2026-09-28: True on seeded-a/b/c, both gemma clusters). Unread; drift facets
    4.1-4.14 omit it.
  - kubectl get pv -o json | jq -r
    '.items[]|select(.spec.gcePersistentDisk!=null)|[.metadata.name,.spec.gcePersistentDisk.pdName,.spec.claimRef.name]|@tsv';
    assert empty unless the add-on is True. Cost SOP 3.2 reads pdName only as a Retain-PV fallback
    handle.
  - kubectl get sc -o custom-columns=NAME:.metadata.name,PROVISIONER:.provisioner; kubectl get pvc
    -A -o json | jq '[.items[]|select(.spec.storageClassName=="standard")]|length'; assert 0. GKE
    ships standard = kubernetes.io/gce-pd beside standard-rwo (default).
- Detect after:
  - kubectl get events -A --field-selector reason=FailedAttachVolume (then FailedMount); kubectl get
    pods -A --field-selector=status.phase=Pending; assert none. gcloud logging read
    'logName="projects/$P/logs/events" AND jsonPayload.reason="FailedAttachVolume"'
  - kubectl -n kube-system get ds pdcsi-node -> NotFound while kubectl get pv -o json | jq
    '[.items[]|select(.spec.gcePersistentDisk!=null or
    .spec.csi.driver=="pd.csi.storage.gke.io")]|length' > 0 is this entry. A VolumeAttachment count
    misses the cordon stall.
  - Re-run the add-on read after every control-plane operation: the flag survives upgrades, so
    not-True plus any PD-backed PV means every re-attach fails from now on. api_deprecation_scan.py
    cannot see it: removed_apis.json keys on apiVersion/kind, not PV fields.
- Fix before:
  - gcloud container clusters update $C --zone $Z --update-addons=GcePersistentDiskCsiDriver=ENABLED
    (Terraform: addons_config { gce_persistent_disk_csi_driver_config { enabled = true } });
    installs pdcsi-node, -rwo classes.
  - Move claims to pd.csi.storage.gke.io: new PVCs on standard-rwo/premium-rwo; for an existing disk
    write a CSI PV (spec.csi.driver pd.csi.storage.gke.io, volumeHandle
    projects/$P/zones/$Z/disks/<disk>), not in-tree.
  - Stop new in-tree objects at the source: grep Git for spec.gcePersistentDisk and provisioner:
    kubernetes.io/gce-pd beside api_deprecation_scan.py; keep the (default) marker off the in-tree
    class (kubectl get sc shows it).
- Fix after:
  - Enable the add-on (command above). The control-plane attacher (no visible pod) processes
    VolumeAttachments and pdcsi-node mounts; on the cordon path the Terminating pod unmounts,
    detaches, the new pod attaches. Time it.
  - If a pod stays ContainerCreating after pdcsi-node is Ready, delete it so kubelet retries:
    kubectl -n intree delete pod -l app=intree-pd; confirm volumeattachment ATTACHED true on the new
    node and disks users names it.

- Cost and time: One zonal cluster, 2 x e2-small + 10 GB pd-standard: cents/hour. Plant: cluster
  create + 5 manifests. Break: add-on op, cordon, delete; first event in ~2 min.
- Exists today: Nothing exists: no fleet role on main or an open pull request, no out-of-tree script
  (gemma-gpu plants API removal and drain, not storage), no kube-agents reader; the catalogue says
  'Read today: nothing'. This recipe is first.
- Caveats:
  - Event texts, the Terminating old pod and the drain wait come from k8s 1.35 source and GKE docs,
    not observation; whether the CSIDriver object and -rwo classes survive the disable is unknown.
    Record all on run one.
  - A fleet role's never-attached disk trips cost 3.4 at D+30 (fleet-cost-idle-pool asserts the
    phrase orphan-pd-, not a count) and leaves a standing stuck pod; both need README background
    rows. Provider: seeded-a only.

### 20. Images on a retired registry

Home: seeded fleet, a new role to plant. A before-signal role fits the read-only fleet: a pause
Deployment on seeded-a referencing k8s.gcr.io, which runs only via the redirect (302 to
registry.k8s.io, 307 to *-docker.pkg.dev, checked 2026-09-28). The break needs a retirable
registry: out of tree.

- Plant:
  - Fleet role: in bench/tf/fleet/defects-a.tf copy the checkout_gateway shape as
    kubernetes_deployment_v1 "legacy_pause": ns seeded-reliability, 1 replica, no Service, container
    pause, image "k8s.gcr.io/pause:3.10", same requests, limits and security_context.
  - fixtures.json role retired-registry-reference: cluster_slot a, ns seeded-reliability, probes
    [deployment/legacy-pause], state {subject deployment/legacy-pause, path
    spec.template.spec.containers[0].image, op eq, value k8s.gcr.io/pause:3.10, why}; README row.
  - Out of tree (Z=us-central1-a): SA=$(gcloud iam service-accounts create retired-reg-nodes
    --format='value(email)'); gcloud projects add-iam-policy-binding $P --member serviceAccount:$SA
    --role roles/container.defaultNodeServiceAccount (no AR permission).
  - Two REGULAR patches: gcloud container get-server-config --location $Z
    (channels[].validVersions); gcloud container clusters create retired-reg --zone $Z
    --release-channel regular --cluster-version NEW --node-version OLD --num-nodes 1
    --service-account $SA
  - gcloud artifacts repositories create retired --repository-format docker --location us;
    ...add-iam-policy-binding retired --location us --member serviceAccount:$SA --role
    roles/artifactregistry.reader; gcloud auth configure-docker us-docker.pkg.dev for crane.
- Break:
  - IMG=us-docker.pkg.dev/$P/retired/pause:3.10; crane copy registry.k8s.io/pause:3.10 $IMG; kubectl
    create deployment legacy-pause --image $IMG; then retire: (a) gcloud artifacts docker images
    delete $IMG --delete-tags, or (b) remove-iam-policy-binding retired.
  - Rehearsal (no skew): gcloud container clusters resize retired-reg --node-pool default-pool
    --num-nodes 2 --zone $Z; kubectl cordon <old node>; kubectl delete pod -l app=legacy-pause.
    Replacement lands on the new node, fails its pull, then ImagePullBackOff.
  - Event: gcloud container clusters upgrade retired-reg --node-pool default-pool --zone $Z --quiet
    (no --cluster-version: nodes take the master version); surge 1/0 adds a node, drains, deletes.
    Pod stays ImagePullBackOff (kubelet retries 10 s to 300 s); op DONE.
- Verify:
  - kubectl get pods -l app=legacy-pause -o
    custom-columns='POD:.metadata.name,NODE:.spec.nodeName,WAIT:.status.containerStatuses[*].state.waiting.reason'
    shows ErrImagePull then ImagePullBackOff, and NODE is the node created after the operation's
    startTime.
  - kubectl get events --field-selector involvedObject.name=<pod>,reason=Failed -o
    custom-columns=MSG:.message reads 'Failed to pull image ... failed to resolve reference', then
    'not found' for (a) or '403 Forbidden' for (b) (GKE image-pull troubleshooting page).
  - Control: with the registry retired and before the cordon, kubectl delete pod -l app=legacy-pause
    once; the replacement on the old node logs Pulled 'Container image ... already present on
    machine' and runs. The cache, not the reference, is what changed.
  - kubectl get node <old> -o jsonpath='{.status.images[*].names}' lists the reference (50 newest
    only); the new node's does not. gcloud container operations list
    --filter='operationType=UPGRADE_NODES' --format='table(status,error.message)': DONE, no error.
- Detect before:
  - Cluster: kubectl get pods -A -o jsonpath='{range .items[_]}{range
    .spec.containers[_]}{.image}{"\n"}{end}{end}' | sort -u | grep '^k8s\.gcr\.io/' (repeat for
    .spec.initContainers and each host being retired); assert no line. Unread by any scheduled
    check.
  - Git: in each linked GitOps repo, git grep -nE 'image:\s*"?k8s\.gcr\.io/'; assert no hit. Unread:
    api_deprecation_scan.py walks the same _.yaml/_.yml/*.json documents but matches apiVersion and
    kind only; a host table beside removed_apis.json is that walk.
  - Egress: gcloud compute firewall-rules list --filter='direction=EGRESS'
    --format='table(name,destinationRanges.list(),denied[].map().firewall_rule().list())'; assert
    k8s.gcr.io, registry.k8s.io and its pkg.dev/S3 backends are admitted. Unread.
- Detect after:
  - kubectl get pods -A -o
    custom-columns='NS:.metadata.namespace,POD:.metadata.name,NODE:.spec.nodeName,WAIT:.status.containerStatuses[*].state.waiting.reason'
    | grep -E 'ImagePullBackOff|ErrImagePull'; assert none; any hit's node postdates the operation.
    Unread.
  - kubectl get events -A --field-selector reason=Failed -o
    custom-columns='OBJ:.involvedObject.name,MSG:.message' | grep 'Failed to pull image' names the
    refusing host. The Cluster Agent's gke-workload-troubleshooting skill reads events only when
    asked.
  - gcloud container operations list --filter='operationType=UPGRADE_NODES' shows DONE regardless,
    and the catalogue records no GKE recommender insight for this entry; assert on the pods and
    events, never on the operation.
- Fix before:
  - Mirror: MIRROR_PREFIX=<registry you own> make mirror-images copies every image in images.json
    (install images only); for workload images, crane copy <old ref> <your registry>/<name>:<tag>,
    the tool the script prefers.
  - Change the reference in Git (image: <your registry>/pause:3.10) and admit the new host in the
    egress rules in the same change; kubectl rollout restart deployment/legacy-pause proves it pulls
    while old nodes still exist.
  - Rerun the three detect_before reads afterwards; an empty result on all three is the acceptance
    test that nothing still names the old host.
- Fix after:
  - Retag: kubectl set image deployment/legacy-pause pause=<your registry>/pause:3.10 -n <ns>, then
    the same in Git; kubectl rollout status confirms. No rollback restores a cache: a pool downgrade
    rebuilds nodes again.
  - Allowlist variant: re-admit the host (the add-iam-policy-binding from plant in the simulation;
    the firewall rule in real life). The kubelet retries within 300 s; kubectl delete pod forces it
    now.

- Cost and time: Fleet role: one pause pod on the standing fleet, no new cost, about an hour to
  write and apply. Out of tree: e2-small zonal cluster, node SA, AR repo, ~40 min.
- Exists today: Nothing: no fleet role on main or in the open pull requests, no out-of-tree script.
  gemma-gpu pulls from us-docker.pkg.dev and docker.io; its only egress control is a pod
  NetworkPolicy, which kubelet pulls bypass.
- Caveats:
  - Pod NetworkPolicy and FQDNNetworkPolicy do not govern kubelet pulls (node network); the VPC
    firewall/NAT path is the allowlist the IAM variant stands in for. The fleet role adds one SOP
    3.10 probes-liveness finding.
  - k8s.gcr.io cannot be retired on demand; it served pause:3.10 via redirect on 2026-09-28. If
    Google drops the redirect the fleet pod goes ImagePullBackOff, the real failure, so the role's
    README row must say so.
