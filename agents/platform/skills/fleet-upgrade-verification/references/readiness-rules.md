# Readiness rules, one per catalogue entry

The per-entry rules `fleet_upgrade_report.py --readiness` runs after the PodDisruptionBudget,
webhook, maintenance and skew rules: one module per entry of the repository's upgrade-failure
catalogue (its design document names the twenty-six; entries 21 to 26 are the support-case rules, each reading a list the report makes once per project or per location) under `scripts/readiness_rules/`, registered in
`EXTRA_RULES` in `scripts/upgrade_readiness.py` in catalogue-entry order, which is the order the
table and the JSON list their findings in. The package docstring in
`scripts/readiness_rules/__init__.py` is the contract a module meets. A rule that can block joins
the member's `blocked` verdict with a blocker and its `unknown` verdict with a finding it could
not make; a rule that cannot block files risks and notes, and an unknown it files goes in the
note and never moves the verdict. The table prints a blocker or a risk under `other blockers` or
`risks` as `<rule id>: <the rule's description>`, so the cell names the rule the way the note
column does.

One entry per rule, in catalogue-entry order: its id and name, the catalogue entry, the highest
grade it files, what it reads and how it grades, and what the cell tells the operator to do. A
new rule adds its entry in that order.

- `surge-capacity` (Surge capacity), entry 2, blocker. Reads: A pool whose upgrade settings take a
  node away before its replacement exists (`maxUnavailable` above 0 on the surge strategy, or a
  blue-green upgrade with the autoscaled rollout policy), measured by placing the pods of each of
  its nodes in turn, largest first, onto the schedulable nodes left that each pod may schedule on
  (its node selector, its required node affinity and the taints it tolerates decide which; a tainted
  pool's spare capacity never counts for a pod without the toleration), each onto the eligible node
  with the most room left by CPU and memory requests whose extended resources (`nvidia.com/gpu`,
  `google.com/tpu`) are free too (native sidecars and the pod's `overhead` counted as the scheduler
  counts them), DaemonSet and mirror pods excepted; a pod no eligible node can take is stranded. It
  blocks when `maxSurge` is 0, `maxUnavailable` above 0, the autoscaler cannot grow the pool (at its
  ceiling, or not autoscaled) and a pod is stranded; a stranded pod with some surge left, a growable
  autoscaler or an autoscaled blue-green rollout is a risk; a pool whose every node's pods are
  placed is a note. The node measured is the one whose stranded pods are the largest in either
  resource, so a memory-bound node counts as a CPU-bound one does, and the cell names the pool, its
  settings, that node, the stranded requests, the room on the nodes the pods may schedule on, the
  largest stranded workloads and any other node of the pool that strands pods. Autopilot members are
  not graded. A pool at or above the target is not graded; no target, an unparsable pool version or
  quantity, or Nodes or Pods not read is unknown, and the rule can block, so that unknown moves the
  verdict. GKE's defaults (`maxSurge` 1, `maxUnavailable` 0) never trigger the placement half. The
  same pools are read for the surge node that must exist before a drain: every pool below the target
  on the surge strategy with `maxSurge` above 0 (the default is 1; a blue-green pool creates its
  green pool instead and is not counted). A pool carrying accelerators (`config.accelerators`) that surges with no reservation affinity (`config.reservationAffinity` consuming neither `ANY_RESERVATION` nor `SPECIFIC_RESERVATION`) is a risk, not a blocker: a surge node needs accelerator capacity the zone may not have, and a pool upgrade that cannot create one waits between versions with the old node serving (catalogue entry 2); one under a reservation is a note. Per Pod range (the pool's `networkConfig.podIpv4CidrBlock`,
  else the cluster's `clusterIpv4Cidr`), the free aligned blocks of the pools' per-node size (each
  node's `spec.podCIDR`, which the projected node read carries, else the block GKE sizes for the
  pool's pod limit; the largest block among the pools is the unit) are counted against the sum of
  the pools' `maxSurge`, and fewer free blocks than surge nodes blocks: a surge node with no block
  never registers. The cell names the range with its free, held and total blocks, the surge need and
  the pools, or the pool with its accelerators and `maxSurge`, and the action: lower `max-surge` or
  add a secondary Pod range; hold a reservation for the surge node, or set the in-place strategy
  (`max-surge` 0, `max-unavailable` 1) on a pool with a spare accelerator node for the drained pods.
  No target is one unknown for the surge pools; Nodes not read, or a record with no Pod range, is
  unknown for the range half while the accelerator half still grades. Quota is not read here: the
  stockout-prevention audit reads it. Action: Keep `maxSurge` at least 1 and `maxUnavailable` 0 with
  one node of headroom, or give the stranded pods a node they may schedule on. For a short Pod
  range, lower `max-surge` or add a secondary Pod range. For an accelerator pool, hold a reservation
  for the surge node, or set the in-place strategy (`max-surge` 0, `max-unavailable` 1) only on a
  pool with a spare accelerator node for the drained pods: on one whose accelerators are all
  requested, in-place strands them, the placement half's blocker.
- `data-on-the-node` (Data on the node), entry 4, risk. Reads: Deployments and StatefulSets are read
  for state, not scratch: a StatefulSet's `emptyDir` (its identity survives the rebuild, its
  node-local data does not), a `hostPath` a container mounts writable, a `hostPath` under a Local
  SSD mount, or a claim (or a StatefulSet's claim template) on a StorageClass whose provisioner is
  `kubernetes.io/no-provisioner` (how Local SSD becomes a PersistentVolume) is a `risk`, named with
  the volume; a volume name that suggests state is marked. A Deployment's `emptyDir`, a
  memory-backed `emptyDir`, a `hostPath` mounted read-only and a `hostPath` declared but mounted by
  no container are notes. Action: Keep state on PersistentVolumes or object storage; node-local disk
  only for what the workload can rebuild.
- `removed-api-callers` (Removed-API callers), entry 6, blocker. Reads: From the removed-release
  audit read: each caller is one principal, user agent and API (`core/v1 endpoints`). The operator's
  callers are user accounts, service accounts, and Kubernetes service accounts outside the system
  namespaces (`kube-system`, `kube-public`, `kube-node-lease`, `gatekeeper-system`, `cnrm-system`,
  `asm-system`, and the `gke-`, `gmp-` and `config-management-` namespaces); every other `system:`
  principal, the node identities and Google-managed service agents are the provider's. An operator
  caller of an API removed after the running control plane's minor and at or before the target's
  whose last write is within 48 hours blocks, named with the release and, where `removed_apis.json`
  knows one, the replacement; one silent for longer is a risk naming its last write, since a
  migrated caller must not block for a week. A removal after the target, a provider caller (GKE
  moves its own components) and a principal the rule cannot place are risks, never blockers; a
  removal the control plane has already crossed is a note, since the API is gone and the window's
  writes predate the crossing. A read that failed or timed out is `unknown`, graded on the pages
  before it; a sampled read is a note. Action: Migrate the caller before the upgrade: bump the
  client, rewrite the manifest or the Helm release state to the replacement API the finding names.
- `changed-defaults` (Changed defaults), entry 8, risk. Reads: The minors the upgrade crosses, from
  the control plane for an admission default and from the lowest node pool for a kubelet default,
  are looked up in `upgrade_shape_tables.DEFAULT_CHANGES_BY_MINOR`, each row with its source (the
  Pod Security Standards page, the Kubernetes 1.33 changelog, the 1.36 release notes). A namespace
  whose `pod-security.kubernetes.io/enforce-version` is `latest` or absent follows the running
  minor; where a crossed minor tightens the level it enforces (1.23 restricted: no `runAsUser: 0`;
  1.34 baseline: no host field on probes), it is a risk naming the namespace and the setting; a
  minor that only widens an allowlist is a note; a namespace pinned to a named version is the safe
  case and is counted. A workload with a `gitRepo` volume is a risk across 1.33 (disabled by
  default) and 1.36 (permanently). A target past the table's `as_of` (1.36) is graded on the minors
  the table covers, with a note that the table ends there; a namespaced read that failed leaves the
  labels ungraded, in the note, since the rule never blocks. Action: Pin the namespace's
  `pod-security.kubernetes.io/enforce-version` to a named minor, or run the crossed minor's level in
  warn and audit before enforce; replace a `gitRepo` volume with an init container.
- `deprecated-api-callers` (Deprecated-API callers), entry 9, risk. Reads: From the
  deprecated-or-kubectl audit read: each operator caller of an API stamped `k8s.io/deprecated` with
  no removal release is a risk named with its principal, user agent, API and, for Endpoints, the
  successor (EndpointSlice); a principal the rule cannot place is a risk naming it. The provider's
  callers the filter did not express are counted in the note, not filed; kube-system's
  endpoint-controller is stamped on every Service's Endpoints it writes, so the label alone
  identifies nothing. Action: Plan the migration while the feature still works (EndpointSlices for
  Endpoints); nothing breaks at this upgrade.
- `client-addon-skew` (Client and add-on skew), entry 10, risk. Reads: kubectl is supported within
  one minor of kube-apiserver (`upgrade_shape_tables.KUBECTL_SKEW_SOURCE`, the Kubernetes version
  skew policy): a `kubectl/vX.Y` user agent in the deprecated-or-kubectl read, or a workload image
  whose repository ends in `kubectl`, further than one minor from the target is a risk naming the
  version and the principal or workload. A client-go user agent carries the binary's version, not
  the library's, so those callers are counted, not graded; the log carries writes, so a kubectl that
  only reads is not in it. `addonsConfig` names the GKE-managed add-ons that are on; they expose no
  version and GKE owns them, so they are listed in the note and not graded. A third-party add-on
  recognised by image repository is graded against `upgrade_shape_tables.ADDON_SUPPORT` (the
  vendor's page and the day it was read): a target outside the release's supported range is a risk;
  an add-on with no table, or at a release the table does not list, is a note ("version not
  graded"). Nothing is inferred. Action: Upgrade kubectl to within one minor of the target and the
  add-on to a release whose support range includes it; check an unlisted add-on against the vendor's
  matrix.
- `zonal-control-plane` (Zonal control plane), entry 11, risk. Reads: A cluster whose `location` is
  a zone is a risk on every report, never a blocker: the Kubernetes API is unreachable for a short
  window while the single control-plane replica is rebuilt, running pods are unaffected, and the
  cell says what to do about it. A location that is neither a zone nor a region is unknown, and
  since the rule can never block, that unknown goes in the note and leaves the verdict to the other
  rules. Action: Schedule the control-plane upgrade in a maintenance window and have API clients
  retry with backoff; only a regional cluster keeps the API available.
- `removed-node-label` (Removed node label), entry 12, blocker. Reads: Every `nodeSelector` and
  required node affinity term that selects nodes carrying a label (`In`, `Exists`) is read against
  the Node list; `NotIn`, `DoesNotExist`, `Gt`, `Lt` and preferred terms never strand a pod and are
  not graded. A label or label value in the drop table (`node-role.kubernetes.io/master` from 1.24,
  `cloud.google.com/gke-container-runtime=docker` from 1.24) is `blocking` only when a pool the
  template can land on crosses the drop in this upgrade (its current minor below the drop, the
  target at or past it); a drop every such pool passed already is a note, since the pods either run
  or are already unschedulable and the upgrade changes neither. A label that no pool's
  `config.labels` defines and no node in the template's pools carries today is a `risk`, named as
  such; a key GKE sets from the pool's configuration (the pool, accelerator, OS and machine labels)
  or one a pool's `config.labels` defines is a pool shape, never this rule's risk even with no node
  today, so a GPU selector with no GPU pool is entry 18's finding; a deprecated beta label the
  kubelet still sets (`beta.kubernetes.io/arch`, `failure-domain.beta.kubernetes.io/zone` and their
  siblings) is a `risk`, named with its GA replacement. The carried question is not asked on
  Autopilot, nor for a pool that is empty today and scales from zero (a note), and it is `unknown`
  when the Node read failed. Action: Replace the label in the selector with its GA name
  (`kubernetes.io/arch`, `topology.kubernetes.io/zone`) before the pool crosses the drop.
- `container-runtime` (Container runtime), entry 13, risk. Reads: For each pool below the target,
  the containerd major its nodes report (`status.nodeInfo.containerRuntimeVersion`, or the table by
  the pool's minor when the nodes were not read or no node of the pool reports a runtime, saying
  which) against the major the target's node image ships
  (`upgrade_shape_tables.CONTAINERD_MAJOR_BY_GKE_MINOR`: Linux images at 1.33, Windows Server at
  1.35, GKE's containerd 2 page as the source), paired with every pod or template that mounts a
  runtime socket (`/run/containerd/containerd.sock`, `/var/run/docker.sock`, either spelling) or the
  directory it sits in (`/run`, `/var/run`, `/run/containerd`, `/var/run/containerd`) through a
  `hostPath` volume. A pool whose major changes under socket clients is a risk naming the pool, both
  majors and the clients' owners; a change with no client, and clients with no change, are notes; a
  change on a member whose Pods or DaemonSets were not read is unknown, and since the rule can never
  block, that unknown goes in the note and leaves the verdict to the other rules. Action: Move the
  agent to the CRI v1 API, or a build for the target's containerd major, before the pool's major
  changes under it; a completed pool can be downgraded in place while GKE still offers the previous
  version.
- `cgroup-v2-runtime` (cgroup v2 under an old runtime), entry 14, risk. Reads: A Java image tag
  below JDK 8u372, 11.0.16 or 15, or a .NET image tag below 5.0 (a .NET Framework image is outside
  the rule: Windows containers only, on pools with no cgroup), on a pool the target moves to cgroup
  v2 (`effectiveCgroupMode` v1 and a target at or past GKE's 1.33 migration, or a pool pinned to v1
  and a target at or past the 1.35 removal) is a `risk`; a known runtime whose tag names no version
  on such a pool, or a pool whose mode the record does not carry, is `unknown`; a pool already on
  cgroup v2, or staying on v1 at the target, is a note. Action: Move to a runtime that reads cgroup
  v2 (JDK 8u372, 11.0.16 or 15 and later; .NET 5 and later) or set the heap explicitly; until 1.35 a
  pool can be pinned to cgroup v1 to buy time.
- `group-oom-kill` (Group OOM kill), entry 15, risk, a heuristic. Reads: A heuristic, marked as one
  in every finding, because the process count is not readable from the API: a container whose
  command or args run `supervisord`, `s6-svscan` or `runsvdir`, whose shell `-c` script forks a
  background job, or whose image is a documented multi-process base, on a pool where the target
  turns the group kill on (a move to cgroup v2 at 1.28 or later, or a cgroup v2 pool's kubelet
  crossing 1.28) is a `risk`, never `blocking`. A pool already group-killing, or one whose node
  config sets `singleProcessOOMKill`, is a note. A `-c` script the rule cannot read (an unterminated
  quote, heredoc or expansion) is `unknown` on a pool where a fork would have been a risk, never
  clean. Action: Raise the limit for the multi-process container, split the workers into their own
  containers, or set `singleProcessOOMKill` in the pool's node system config.
- `network-dataplane` (Network dataplane), entry 16, risk. Reads: A cluster on the legacy dataplane
  (`networkConfig.datapathProvider` absent or `LEGACY_DATAPATH`) whose network policy add-on is off
  and whose namespaces carry NetworkPolicies is a risk naming the namespaces and their counts:
  nothing enforces those policies today, and all of them take effect the moment enforcement does, at
  the node rebuild after the add-on is enabled or on the Dataplane V2 cluster that replaces this
  one, which is the shape GKE's recommender files as `NETWORK_POLICIES_UNRECONCILED`; the cell names
  the Dataplane V2 members among the clusters the run listed (the projects' whole listing, even when
  `--cluster` narrows who is graded), where the same policies would be enforced. Policies the add-on
  enforces are a note, and so is a legacy member with no policy at all, so every legacy member
  states its dataplane and enforcement; a Dataplane V2 member is clean; a legacy member whose
  NetworkPolicies were not read is unknown, which never moves the verdict since the rule cannot
  block. Action: Test what the policies deny before enabling the network policy add-on or moving to
  Dataplane V2; keep NetworkPolicy explicit.
- `node-image-coupled-agent` (Node-image-coupled agent), entry 17, risk. Reads: A DaemonSet with
  `hostNetwork: true`, outside the system namespaces, that selects on a node-image label
  (`cloud.google.com/gke-os-distribution`, `cloud.google.com/gke-container-runtime`), mounts the
  kernel module tree or the network plugin's directories (`/etc/cni/net.d`, `/opt/cni/bin`,
  `/home/kubernetes/bin`), or runs `modprobe` or `insmod`, in its command or args or in a script
  they hand a shell through `-c`, is a `risk`; the detail names the pools below the target, whose
  image this upgrade changes. Action: Upgrade a canary pool first and watch Service routing from
  inside the cluster; surge with `maxUnavailable` 0; downgrade the pool while the previous version
  is offered if the agent fails on the new image.
- `gpu-driver` (GPU driver mismatch), entry 18, blocker. Reads: For a template requesting
  `nvidia.com/gpu`, the CUDA version is read from the image tag or from the `CUDA_VERSION` or
  `NVIDIA_REQUIRE_CUDA` env value (no other env: a compute-capability list is not a toolkit
  version), and the driver from the pools carrying the accelerator the template selects: the pool's
  `imageType` and `gpuDriverVersion` (`DEFAULT` or `LATEST`) pick a branch from GKE's per-minor
  table at the target's minor. On Autopilot GKE provisions the node itself, on Container-Optimized
  OS with the driver the template's `cloud.google.com/gke-gpu-driver-version` selector names
  (`default` when absent), so the same table answers with no pool. A target driver below the CUDA
  major's floor is `blocking` when the pool's running node image (the same table at the pool's own
  minor) serves the pin, and a `risk` naming both branches when the running driver is below the
  floor too, since the device does not open today and the upgrade does not change that (Autopilot,
  with no pool to read, and a running minor the table lacks grade the target alone); one below the
  toolkit's own minimum is a `risk` (minor version compatibility); an image naming no CUDA version,
  or a Standard template whose accelerator no pool carries, is a `risk`; a target past the driver
  table's last row (1.33 today, as GKE's GPU page's; read 2026-10-20) is a `risk` naming the page to
  verify the floor against; a target below the first row, an operator-installed driver, or a record
  with no install mode is `unknown`. A pin the driver serves is a note, with the reminder that
  forward-compatibility packages inside the image are not readable from the tag. Action: Match the
  driver to the images before the upgrade (GKE's driver table at the target minor against NVIDIA's
  minimum-driver matrix), or recreate the pool with the driver the images need; upgrade a canary GPU
  pool first.
- `in-tree-volumes` (In-tree volumes), entry 19, blocker. Reads: Every PersistentVolume with an
  in-tree `gcePersistentDisk` source, the claim bound to it (the volume `Bound` and the claim its
  `claimRef` names carrying `spec.volumeName` for it; a `Released` volume's stale `claimRef`, or a
  claim since rebound to another volume, counts no consumer and is a note), and the pods and
  templates that mount the claim, against `addonsConfig.gcePersistentDiskCsiDriverConfig.enabled`.
  With the add-on off, a volume a workload mounts blocks (the rescheduled pod cannot attach it),
  naming the volume and the workload, and an unmounted one is a risk; with the add-on on, every
  in-tree volume is a risk, since it still depends on the migration shim rather than on the driver.
  PersistentVolumes not read is unknown and, since the rule can block, moves the verdict; Pods not
  read is unknown too, with the templates still graded. Action: Enable the PD CSI driver add-on
  before the upgrade, and move the volume's StorageClass to `pd.csi.storage.gke.io`.
- `retired-registry` (Retired registry), entry 20, risk. Reads: A container or init container
  pulling from `k8s.gcr.io`, `gcr.io/google-containers`, `gcr.io/google_containers` or
  `gcr.io/kubernetes-helm` is a `risk`, named with the image, the host and what the host does today:
  a rebuilt node has no image cache and pulls from a host that stopped publishing, which still
  answers (`k8s.gcr.io` by a redirect to registry.k8s.io) until it does not. The detail names the
  pools below the target, whose nodes this upgrade rebuilds, or says no pool is below it yet.
  Action: Mirror the image into a registry you own and retag the reference; keep egress allowlists
  in step.
- `lb-connection-draining` (Load balancer connection draining), entry 21, blocker. Reads: `gcloud compute backend-services list` once per project: each backend on the member's `nodePools[].instanceGroupUrls` or on a NEG its Services annotate, with `connectionDraining.drainingTimeoutSec` above 600, GKE's per-node deregistration wait, blocks, one blocker per backend service, naming the service, its timeout and the pool or NEG. Action: `gcloud compute backend-services update <name> --global|--region <region> --connection-draining-timeout=600`.
- `orphan-node-objects` (Orphan Node objects), entry 22, blocker. Reads: `gcloud compute instances list` once per project against the member's Nodes (`spec.providerID`'s zone and instance name): a Node whose instance the project no longer has blocks; a Node created after the list's read time (`metadata.creationTimestamp`), whatever its readiness, and a `Ready`, uncordoned Node the list does not carry (the list and the node read are minutes apart) are notes naming the instants, not blockers; the note counts nodes against instances on every member. Action: `kubectl delete node <name>`; nothing runs on it.
- `control-plane-upgrade-history` (Control-plane upgrade history), entry 23, risk, or a note when the window is off-peak. Reads: `gcloud container operations list` once per project: `DONE` `UPGRADE_MASTER`, `UPGRADE_NODES` or `REPAIR_CLUSTER` operations on the member or on one of its node pools in the last 90 days whose error names capacity, against the cluster's `maintenancePolicy.window`, its opening placed in the region's standard local time against the busy hours 06:00 to 21:00. Action: with no window, or one that opens in the busy hours, set a maintenance window in an off-peak hour for the region and retry the upgrade in it; with a window that opens off-peak, retry the upgrade in it; either way, keep upgrade notifications consumed.
- `cluster-status-age` (Cluster status age), entry 24, blocker. Reads: the cluster's `status` and, from the same operations list, the operations on the member or on one of its node pools (a pool upgrade, resize or repair names the pool); a status other than `RUNNING` with an operation `RUNNING` for more than four hours blocks, naming it, a younger one is noted; no `RUNNING` operation in the project's list is read again for the member, a `clusters describe` and then an `operations list` for the cluster (the project's lists may predate the operation's end, or its start): `RUNNING` now is a note, a `RUNNING` operation in the fresh list is graded against the threshold, a failed read is unknown, and nothing running with a status still not `RUNNING` blocks, quoting the cluster's `statusMessage`. Action: freeze changes; past a few hours open a support case quoting the operation id, or the status and its message when nothing is `RUNNING` (the `gke-upgrades` skill's "Stuck operation" section).
- `target-patch-valid` (Target patch valid), entry 25, blocker. Reads: `gcloud container get-server-config` once per location: the member's target against its channel's `validVersions`, and against `validNodeVersions` too only when the control plane is already at or above it (a pool move; a blocker otherwise; a target given without its `-gke.` build is valid when a list carries a build of it), and its running control-plane version against the channel's `validVersions` (a risk). Action: target the newest active patch of the minor on the channel at or above the version, else the next active version the channel lists.
- `known-issues-matrix` (Known-issues matrix), entry 26, risk. Reads: the DaemonSets and Deployments in `kube-system` and `gmp-system` (the reads the report makes; `addons` in the JSON) against `scripts/known_issues.json` (the page's rows naming a minor from 1.32 up; a row naming no add-on matches by version alone; a match through a version set with no upper bound, `All versions` or a build or minor and later with no fixed build above it, is a note on the member, not a risk; a target given without its build is graded against the builds of the patch `target-patch-valid` holds valid, the channel's list, `validNodeVersions` too when the pools alone move: a risk when the issue affects every one and a note naming the affected builds otherwise), and the table's age, a risk past thirty days. Action: target the patch the issue names as fixed, or hold; regenerate a stale table before trusting a clean matrix.

A blocking rule declares what its mechanism holds (`HOLDS`): a control-plane move, a node-pool move, or both. The fold stamps it on each `rule_blocking` row as `holds`, and the Monday audit's `upgrade-blocked` check counts the block for the move the cluster's lag makes due: `lb-connection-draining` and `orphan-node-objects` hold a node's removal and do not count where the control plane alone is due, as a budget does not; `cluster-status-age` and `target-patch-valid` hold both moves. A blocking rule's `unknown` (a read it needed failed) carries `holds` too, and the check records the member unevaluated for it on the move due, never clean; a block on the row still wins.

Once per project is once per reporter process: the skill's fleet run and the daily watch grade a project in one process and make the lists once; the Monday audit runs the reporter once per behind cluster and makes them per cluster.

A failed project or location read is an error row under the table (exit 1), and the rules that need it say `unknown`; a rule that can block then leaves the member `unknown`, one that cannot is reported in the note. The three list reads are projected to the fields the rules use, so a project of a few thousand instances stays under the credential proxy's output cap; a list the proxy cut is the same error, naming the cap. The three list reads need `roles/compute.viewer` (backend services, instances) and `roles/container.viewer` (operations) in each project the report reads; the default role list carries both. A custom list without them leaves the rules that need the read `unknown`, the member `unknown` where the rule can block, and the run at exit 1.
