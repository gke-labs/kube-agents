# SOP: Fleet Stockout Prevention & Capacity Audit (Daily Governance)

**Purpose:** Sweep every managed GKE cluster and GCP region for capacity stockout vulnerabilities, fragile ComputeClass configurations, quota bottlenecks, and obtainability risks before workloads suffer scheduling outages. The question this audit answers for a platform admin is: _which workloads and compute classes on my fleet will fail to scale or encounter a capacity stockout during demand spikes or zonal hardware shortages?_ Output is this stream's single GitHub ledger issue, rewritten in place on every run, plus narrow remediation Pull Requests carrying generated manifests for the findings that get promoted.

**Cron:** id `stockout-prevention`, schedule `20 9 * * *` (daily 09:20 UTC). The id is a stable observability identifier and does not change.

**Data sources:** `kubectl` read verbs, `gcloud compute ...`, `gcloud container ...`, GCP reservations API (`gcloud compute reservations list`), Spot capacity advice APIs (`gcloud beta compute advice capacity`, `gcloud beta compute advice capacity-history`), and Cloud Logging autoscaler visibility logs (`container.googleapis.com/cluster-autoscaler-visibility`). **Nothing else** — no external blueprints, no manual assumptions. Every conclusion is derived from live cluster and cloud reads you performed in this run.

---

## Execution Checklist

### 0. Open the audit run

```bash
python3 ./skills/fleet-audit/scripts/audit_report.py start --audit stockout-prevention [--repo "<owner>/<repo>"]
```

If multiple repositories are registered in `$GITOPS_STATE_CONFIGMAP` (`managed_repos`), pass `--repo "<owner>/<repo>"` explicitly:

- **Interactive session:** If no `--repo` was specified, prompt the user to choose which repository to target before proceeding.
- **Scheduled / unattended cron:** Iterate over all repositories in `managed_repos` in sequence, executing the audit and running `audit_report.py start` and `audit_report.py finish` for each repository with `--repo "<owner>/<repo>"`.

Returns `{"issue": <int|null>, "repo":"org/repo", "workspace":"/opt/data/gitops/stockout-prevention/org__repo", "findings_path":"/opt/data/scratch/findings_stockout-prevention.json", "pending_remediation_requests":[…]}`. Keep `findings_path` and `workspace` from this call; you write into both.

- `workspace` is the GitOps clone `start` made for you. The audit pod does not begin life inside a checkout, so this is the only tree that exists, and every `remediation.path` in Step 4 is resolved against it — a manifest written elsewhere is one the harness cannot find, and for a fix the sweep would open, `finish` refuses until it is written or passed to `--decline-fix`.
- `issue` is this stream's open ledger issue, or `null` when it has none. Either way you never create it — `finish` owns that.
- `pending_remediation_requests` lists finding ids a repo writer asked for with a `/remediate` comment on the ledger. Write a manifest for each one while you inspect (Step 4), or the promotion fails for want of a file.
- `start` creates and resets no branch. There is no report branch.

The helper owns every git and forge operation and renders the ledger issue body and every remediation PR body — **never hand-write an issue or PR body, never run `git commit`, `git push`, `vcs.py issue create`, `vcs.py proposal create`, or `vcs.py issue comment` yourself, and do none of it any other way.**

**Never comment on the ledger yourself.** `/remediate` is a human reviewer's instruction to this harness, not a step in the audit: an agent that posts it is authorizing its own pull request.

### 1. Enumerate the target fleet

```bash
gcloud config get-value project
gcloud projects list --format="value(projectId)"
gcloud container clusters list --project=<project> --format=json   # for the active project and every listed one
```

A project whose own Kubernetes Engine API is disabled holds no cluster; its `clusters list` failure is that answer, not an unreadable project. That holds only when the refusal names this project: one naming another project, such as the credential's quota project, says nothing about this one's clusters, and the collector records it as a failed list.

- Target every cluster whose `status` is `RUNNING` or `RECONCILING`. A reconciling cluster is not one you cannot read: GKE sets that status while work proceeds on an otherwise-operational cluster, and the API server stays up throughout. It is also ordinary — any config change puts a cluster there for minutes — so excluding it drops clusters from the audit at random, with no check evaluated against them. `PROVISIONING` has no API server yet and `STOPPING` is on its way out; both are recorded rather than audited. Record `{name, location, project, checks_run}` into `scope.clusters`.
- Obtain per-cluster credentials into an isolated kubeconfig so clusters cannot bleed into each other:
  ```bash
  export KC="${HERMES_HOME:-/opt/data}/.kubeconfigs/kubeconfig_<project>_<cluster>_<location>.yaml"
  KUBECONFIG=$KC gcloud container clusters get-credentials <cluster> --location=<location> --project=<project>
  ```
- **`checks_run` is mandatory on every cluster,** and each entry is an object, never a bare string:
  ```json
  {
    "check": "ccc-missing-fallbacks",
    "command": "kubectl --context prod-usc1 get computeclasses -A -o yaml"
  }
  ```
  `check` is the backticked slug from the §3 heading that defines it — `ccc-missing-fallbacks`, `ccc-no-ondemand-floor`, and so on. `command` is the literal invocation you issued on that cluster for that check. It must name one of `kubectl`, `gcloud`, `gsutil`, `bq`, `helm`, or `curl`; anything under eight characters is rejected.
- **A check the cluster's shape rules out is declared in `checks_not_applicable`** with a specific reason:
  ```json
  {
    "check": "single-zone-nodepool",
    "reason": "Cluster is GKE Autopilot mode; node pool management is fully delegated to GKE."
  }
  ```

### 2. Collect capacity and workload state

Collect the live cluster definitions, GCP reservations, regional capacity metrics, and autoscaler visibility logs:

```bash
# 1. Dump ComputeClasses, Workloads, and StorageClasses
KUBECONFIG=$KC kubectl get computeclasses,deployments,statefulsets,storageclasses -A -o json > /opt/data/scratch/stockout_state_<cluster>.json

# 2. Inspect GCP Reservations (guaranteed capacity anchors)
gcloud compute reservations list --project=<project> --format=json > /opt/data/scratch/reservations_<project>.json

# 3. Inspect GCP Regional Quotas for the cluster's region (e.g. us-central1)
gcloud compute regions describe <region> --project=<project> --format="json(quotas)"

# 4. Check Spot Capacity & Preemption Advice History.
#    One machine type per call -- `--machine-type` is singular. Repeat for each
#    shape the fleet's Spot ComputeClasses actually request.
gcloud beta compute advice capacity-history --project=<project> --region=<region> --machine-type=g2-standard-4 --provisioning-model=SPOT --types=PREEMPTION,PRICE --format=json

# Or check capacity obtainability for target machine types. This is the sibling
# command, and it is the one that takes the plural
# `--instance-selection-machine-types` and `--size`:
gcloud beta compute advice capacity --project=<project> --region=<region> --provisioning-model=SPOT --size=1 --instance-selection-machine-types="g2-standard-4,n4-standard-4,c3-standard-4" --target-distribution-shape=any --format=json

# 5. Autoscaler Visibility Logs (Stage 1 Triage Query)
gcloud logging read 'log_id("container.googleapis.com/cluster-autoscaler-visibility") AND resource.labels.cluster_name="<cluster>" AND resource.labels.location="<location>" AND (jsonPayload.noDecisionStatus.noScaleUp:* OR jsonPayload.resultInfo.results.errorMsg:*)' --project=<project> --freshness=24h --limit=1000 --format=json  # both schemas; see §3.11
```

### 3. Checks

**Run the collector before evaluating a covered check below by hand.**

```bash
python3 ./skills/fleet-audit/scripts/fleet_stockout.py > /opt/data/scratch/manifest_stockout-prevention.json
```

It reads every cluster in every project it can see — the active `gcloud` project plus every project `gcloud projects list` returns, or only the one `--project` names — and runs for minutes, not seconds: give the terminal call `timeout: 600`, the foreground maximum, because a shorter one kills it partway and leaves no manifest. This covers all twelve checks below — the ten built on a `ComputeClass`/`Deployment`/`StatefulSet`/`StorageClass`/`Node` dump, `gcloud container node-pools list`, `gcloud compute reservations list`, or `gcloud compute regions describe --format=json(quotas)`, plus `spot-scarcity-risk` (3.8) off `gcloud beta compute advice capacity-history` and `autoscaler-out-of-resources` (3.11) off `gcloud logging read`. The `Node` dump exists only so 3.9's "`>= 90%` of the pool's _effective_ ceiling" test can count each pool's _live_ nodes — `initialNodeCount` is creation-time and the autoscaler never updates it. It applies the standard exclusions S1–S5 and the non-production rule below itself, so a candidate has already passed them. Some sub-conditions it does not reach — listed below — stay yours to check by hand. Read the manifest before doing anything else:

- Every entry in `manifest.clusters` carries an `outcome`. For `"collected"`, copy its `commands` list verbatim into that cluster's `checks_run`, with a script that loads the manifest and assigns the list rather than by retyping it — the slugs that actually applied there, never a slug this manifest does not list. `"unreachable"` means the collector evaluated no check against that cluster, and its `error` says why. An `"unreachable"` entry whose `error` says the cluster is neither `RUNNING` nor `RECONCILING` is not worth a manual retry, because the state that stopped the collector will stop you too: put it in `scope.skipped` with that reason. Any other `"unreachable"` entry — a `get-credentials` failure on a running cluster — is worth one, and so is `"gate-failed"` on a cluster entry, which means the object dump came back short and the collector refused to score a truncated cluster. For either, fall back to this section's reads for that cluster alone, and give it a `limitations` note saying the collector could not read it: `finish` rejects a `checks_run` on a target the manifest does not mark `"collected"` unless `limitations` says why it is not a full read.
- Every cluster entry also carries `autopilot` and `has_nap` booleans — the mode, and whether node auto-provisioning is on. Both are already resolved, and both ride on the error shapes too, because neither stops being true because a read of the cluster failed. Take them from there rather than re-issuing `gcloud container clusters describe`: `--format='value(autoscaling.enableNodeAutoprovisioning)'` prints an empty line when NAP is off, which reads as unreadable rather than as false and sends you round again for a fact the manifest already gave you.
- Every entry in `candidates` is a verified finding: `check`, `object`, `severity`, and `excerpt` are already computed, including `ccc-no-ondemand-floor`'s escalation to `critical` for an inference workload and `reservation-mismatch-risk`'s two forms (an `Automatic`/`AnyBestEffort` affinity binding under a `ComputeClass/<name>` object, an idle reservation under a `Reservation/<zone>:<name>` object). `quota-exhaustion-risk` names its object `Quota/<region>:<metric>` and `autoscaler-out-of-resources` names its `ScaleUpError/<message-id>`, so two zones, regions or message ids are two findings; copy each `object` as it stands. What is still yours to write is the `recommendation` (§5) and, for a `kind: manifest` remediation, the manifest file itself (§4).
- The manifest also carries a `project/<project-id>` entry for each project in scope, cluster or not, for the two project-scoped checks (`quota-exhaustion-risk`, `reservation-mismatch-risk`'s idle-capacity form). A project with no cluster and Compute Engine off has neither check to run and gets no entry. On a project with no cluster, `quota-exhaustion-risk` arrives in the entry's `checks_not_applicable`, since no cluster there can exhaust a quota; copy it as it stands. A quota or a reservation belongs to a project rather than to one cluster, so a finding for either sets `cluster` to the literal string `project/<project-id>` and leaves `namespace` empty — the entry's `name`, which is what the finding's id is derived from. A `project/<id>` entry whose `clusters list` completed and came back empty, or was refused because that project's own Kubernetes Engine API is off, also carries `clusters_listed: 0`: copy it verbatim onto that project's `scope.clusters` entry, and never write it yourself. It is how `finish` tells a fleet with no clusters from a run that lost them — a run with no cluster target is partial unless every `project/<id>` target carries it, `finish` rejects the marker unless `--manifest-file` is given, and a marker the manifest entry lacks is rejected. A project whose list failed, was refused by another project's API (such as a quota project's), timed out on a zone, or was never reached has no such marker, so its missing clusters stay a gap.
- Pass `--manifest-file <path>` to `finish` (§6) so it cross-checks your `checks_run` against what the collector actually ran.
- A GKE cluster name is unique only inside one project and location, so the collector names every cluster `<project>/<location>/<name>`: in the manifest entry's `name`, and so in `scope.clusters[].name` and every finding's `cluster`. Carry that form verbatim, and use it for a cluster you fall back on by hand too: `finish` matches `scope.clusters[].name` against the manifest, so a name shortened back to the bare one reads as a collected cluster the document omitted. `object` does not take that form: it is the candidate's own spelling, in which `Reservation/<zone>:<name>` and `Quota/<region>:<metric>` carry their location, copied as it stands; commands still take the real `--project` and `--context`. A project whose `clusters list` failed contributes a `project/<id>` entry at `"gate-failed"` — a list another project's Kubernetes Engine API refused (such as a quota project's), a list with a zone that did not respond (the clusters that did arrive are still audited), and a collector crash on that project all count — as does one the collector never reached — it stops starting project reads partway through so the call can end inside its timeout, and that entry's `error` begins `not read:` — and `project/UNENUMERATED_PROJECTS` appears when `gcloud projects list` failed, returned a list that omits the active project, or `--project` confined the run to one project, since each way other projects may have gone unlisted: put either in `scope.skipped` with the collector's `error` as the reason rather than leaving it out, which would read as a project holding no clusters. A manifest with a top-level `error` left nothing to put in `scope.clusters` — no project could be listed or reached, the collector reached none of the clusters, or none holds a cluster or has Compute Engine on — and the collector exits non-zero for it: do not call `finish`, and report the error as your one-line summary.
- The manifest entry may carry `checks_unevaluated`, a list of `{check, reason}` for a check whose own read failed — the node-pool list, the autoscaler log read, or a regional quota or reservations read — or for `spot-scarcity-risk` whenever any Spot shape went unmeasured: an advice read that failed or returned under seven days of preemption history, a shape past the collector's eight-shape ceiling, node pools that could not be read, or a request naming a machine family and no machine type, which `capacity-history` cannot query (the answered shapes' findings are still filed, and the reason names them). Such a check did not run and is not inapplicable either: leave it out of both `checks_run` and `checks_not_applicable`, and carry the entry's `limitations` onto that cluster, which already names it. `finish` rejects a document that claims it either way.
- These sub-conditions are not in the collector; check them by hand, reading each ComputeClass once for all of them:
  - 3.10(b) — a `ComputeClass` targeting a specific reservation that does not exist or sits in an unreachable zone.
  - 3.12(b) — a ComputeClass whose own `status.conditions` reports invalid configuration.
  - 3.12(a)'s namespace default — a namespace labelled `cloud.google.com/default-compute-class` naming a class that does not exist (a built-in named as 3.12's Do NOT flag allows is not missing); namespaces are not in the dump.
  - 3.8's low-obtainability arm — the collector reads `capacity-history` for preemption, never `advice capacity`.
  - 3.3 for a large shape requested by a node pool or a workload rather than a ComputeClass priority, and for accelerator-optimized shapes whose vCPU count the machine name does not carry (`a2-highgpu-8g`, `a3-highgpu-8g`).
  - 3.6 for a `Deployment` that mounts a Hyperdisk claim through `volumes[].persistentVolumeClaim` — the collector resolves Hyperdisk only from StatefulSet `volumeClaimTemplates`, because PersistentVolumeClaims are not in the dump.
  - 3.10(c)'s "while production workloads in the same region run unreserved" clause — the collector flags every idle reservation whatever else runs in its region.
- A cluster's `checks_not_applicable` may declare `spot-scarcity-risk` when nothing on it requests Spot capacity, and its `limitations` may name a Spot priority that gives a machine family but no machine type — `capacity-history` takes one `--machine-type` and has nothing to answer for a family. Carry both into §6 as they stand; neither is a finding. When any Spot request on the cluster is family-only, `spot-scarcity-risk` arrives in `checks_unevaluated` instead, under the bullet above.

**A cluster the collector covered is not a cluster you dump or query again.** The per-check reads below exist for a `"gate-failed"` cluster and for 3.10(b), 3.12(b) and the other sub-conditions listed above that the collector does not reach — never for re-deriving a candidate it already produced, whose evidence `finish` takes from the manifest. The object dump is where that goes wrong at fleet scale: `computeclasses`, `deployments`, `statefulsets`, `storageclasses`, and `nodes` were read once for every `"collected"` cluster, and `gcloud container node-pools list` was run against each of them, so a loop that re-reads any of those across the fleet buys nothing `candidates` does not already hold. Scanning workloads for a `cloud.google.com/compute-class` nodeSelector is 3.12(a)/(c)/(d) and is already done; re-listing node pools is 3.9 and is already done. When 3.10(b) and 3.12(b) send you to the ComputeClass objects, read them once and answer both from that one copy.

Each `project/<project-id>` entry is covered on the same terms. `gcloud compute regions describe --format=json(quotas)` and `gcloud compute reservations list` were run for you, and their candidates are in that entry — re-running either to see the raw numbers reads a quota that has not moved since the collector read it minutes ago, and `check_quota`'s 90% threshold has already been applied to every CPU, GPU and TPU metric the region returns except `COMMITTED_*` — the metrics §3.7 covers; the region's other quotas are outside it.

**Standard exclusions — apply to every check below:**

- **S1 — system namespace:** `kube-system`, `kube-public`, `kube-node-lease`, `gmp-system`, `gmp-public`, `gke-gmp-system`, `cnrm-system`, `configconnector-operator-system`, `krmapihosting-system`, `istio-system`, `asm-system`, `anthos-identity-service`, `gatekeeper-system`, `composer-system`, or any namespace matching `gke-*`, `gke-managed-*`, or `config-management-*`.
- **S2 — GKE-managed object:** carries `addonmanager.kubernetes.io/mode`.
- **S3 — operator-owned:** non-empty `metadata.ownerReferences`.
- **S4 — explicit opt-out:** carries `kubeagents.x-k8s.io/stockout-audit: exempt`.
- **S5 — not running:** `spec.replicas == 0`, or completed batch Jobs.
- **"Non-production" — every check below that names it:** an explicit opt-out (S4), a namespace or workload name containing `test`, `staging`, `stage`, `dev`, `sandbox`, or `qa` as a `-`/`_`-delimited token, or a `resourceLabels`/label value of `environment`/`env`/`stage`/`tier` matching one of those tokens. Anything else is production for this SOP's purposes. The collector applies it, and S1–S5, before emitting a candidate; the lists here are what it tests, and what you test on a manual fallback.

#### 3.1 Lack of fallback machine families and dimension diversity (`ccc-missing-fallbacks`)

- **Reference:** `skills/gke-compute-classes/references/compute-class-prioritization.md`
- **Command:** `kubectl --context <ctx> get computeclasses <name> -o yaml`
- **Flag when:** A `ComputeClass` has `priorities[]` pinned to a single machine family or varies fewer than 2 of the 4 core obtainability dimensions across its priority chain: Zone, Family, Capacity Model (Spot vs. On-Demand), and Machine Size (vCPU core count). A priority's zones are its own `location.zones`, its specific reservations' zones, or else the class-level `priorityDefaults.location.zones`. Zone varies when priorities name different zones, when one priority lists two or more, or when a priority names no zone on a cluster whose node locations, auto-provisioning locations or node pools span more than one zone (Autopilot counts as multi-zone). A `podFamily`, `nodepools` or accelerator-only priority in a chain that also names machines leaves its family and size to GKE or the pools, so they are unknown. When the dimensions left unknown — those, an unread span, an unparsed machine size — could bring a chain to 2, it is listed in `checks_unevaluated` rather than filed.
- **Do NOT flag:** ComputeClasses that vary 2+ obtainability dimensions (e.g. multi-zone `c3` fallback to `n4` and `n2` — every priority on the same zones, or on none in a multi-zone cluster — or Spot fallback to On-Demand across zones); ComputeClasses whose every priority names a `podFamily` rather than a `machineFamily`/`machineType`, which pins no machine family at all and leaves the shape to GKE's capacity broker — this is what the built-in Autopilot classes (`autopilot`, `autopilot-arm`, `autopilot-spot`) do, and they are GKE-managed besides, so a finding against one has no manifest to remediate; standard exclusions.
- **Severity:** `critical`. When GCE encounters a zonal shortage or stockout on that machine family, Cluster Autoscaler has no fallback path and scale-up fails completely.
- **Impact:** "Pinned to a single machine family or narrow configuration: any zonal capacity exhaustion causes scale-up to fail and leaves pods unschedulable."
- **Remediation:** `kind: manifest`. Add multi-zone distribution and secondary fallback machine families (e.g., fallback from `c3` to `n4` and `n2`) to the ComputeClass manifest in GitOps.

#### 3.2 Spot-only ComputeClass without on-demand safety floor (`ccc-no-ondemand-floor`)

- **Reference:** `skills/gke-compute-classes/references/compute-class-prioritization.md`, `skills/gke-compute-classes/references/compute-class-gotchas-and-cuds.md`
- **Command:** `kubectl --context <ctx> get computeclasses <name> -o yaml`
- **Flag when:** A ComputeClass `priorities[]` array contains only Spot instances (`spot: true` or `provisioningModel: SPOT`) with no On-Demand priority rule at the end, or a latency-sensitive inference workload (per the discriminator under **Severity**) selects a class whose first priority is Spot, even with an On-Demand fallback after it.
- **Do NOT flag:** ComputeClasses that contain an On-Demand fallback priority at the bottom of `priorities[]`, unless an inference workload selects one that tries Spot first; the built-in Autopilot class `autopilot-spot`, whose every priority names a `podFamily` and which GKE manages, so the `kind: manifest` remediation below has no manifest to append to — the same exclusion and the same reason as §3.1, and the reason it is scoped to `podFamily`-only chains rather than to the name; workloads the §3 "Non-production" rule covers (name, namespace or environment label, or the S4 opt-out), which also do not escalate a class they select. The one exception is an `autopilot-spot` an inference workload actually selects: the `critical` escalation below describes a real and immediate risk, so it is reported even though its remediation has to be `kind: manual`.
- **Severity:** `major`, escalated to `critical` when a workload referencing an all-Spot ComputeClass is an inference workload per the serving half of the AI Workload Security audit's discriminator (`governance/ai_security_audit_sop.md` §2: a container image naming a known inference or serving runtime, or a container requesting an accelerator; its third prong, a model-provider credential, marks a workload that calls a model rather than one that serves it) — a Spot preemption there breaches a user-facing latency SLA immediately rather than delaying a batch job.
- **Impact:** "If Spot VM capacity is preempted or exhausted in the region, the workload has no on-demand floor and remains permanently in Pending state."
- **Remediation:** `kind: manifest`. Append an On-Demand priority rule at the lowest priority in the ComputeClass manifest to act as a guaranteed capacity floor; for a Spot-first class an inference workload selects, move the On-Demand rule ahead of the Spot ones or give the workload its own On-Demand-first class.

#### 3.3 Large VM shape scarcity (>32 vCPU) without multi-family fallbacks (`ccc-large-vm-scarcity`)

- **Reference:** `skills/gke-compute-classes/references/compute-class-prioritization.md`
- **Command:** `kubectl --context <ctx> get deployments,statefulsets,computeclasses -n <ns> <name> -o yaml`
- **Flag when:** A workload or ComputeClass requests very large VM sizes (>32 vCPUs, such as `m1-ultramem-160`, `c3-highcpu-88`, `a2-highgpu-8g`) from thin capacity pools without secondary fallback families or horizontal replica spreading.
- **Do NOT flag:** Workloads requesting standard/horizontal shapes (<=32 vCPUs); stateful monolithic databases that explicitly declare multi-region failover.
- **Severity:** `major`.
- **Impact:** "Very large VM shapes (>32 cores) draw from thin regional capacity pools and are highly prone to sudden stockouts during scale-up."
- **Remediation:** `kind: manifest`. If horizontally scalable (verified with `kubectl top pod`), propose smaller replica shapes with horizontal autoscaling; otherwise add fallback machine families in GitOps manifests.

#### 3.4 Excessive priority rules causing autoscaler backoff loops (`ccc-priority-starvation`)

- **Reference:** `skills/gke-compute-classes/references/compute-class-prioritization.md`, `skills/gke-compute-classes/references/compute-class-debug.md`
- **Command:** `kubectl --context <ctx> get computeclasses <name> -o yaml`
- **Flag when:** A `ComputeClass` contains more than 10 total priority rules (granular `machineType` rules or family entries), exceeding Flex Advisor combinations and triggering Cluster Autoscaler cooldown/backoff reset loops.
- **Do NOT flag:** ComputeClasses using <= 5 broad `machineFamily` level definitions (e.g. `n4`, `c3`, `n2`).
- **Severity:** `critical`.
- **Impact:** "Excessive priority rules (>10) exceed the autoscaler solver cache limit, triggering backoff loops that starve lower priorities."
- **Remediation:** `kind: manifest`. Auto-compress the ComputeClass: replace granular rules with 3-4 family-level (`machineFamily`) priority rules.

#### 3.5 Mixed disk generations on PV-attached ComputeClasses (`ccc-mixed-disk-generations`)

- **Reference:** `skills/gke-compute-classes/references/compute-class-gotchas-and-cuds.md`, `skills/gke-compute-classes/references/compute-class-provisioning-methods.md`
- **Command:** `kubectl --context <ctx> get computeclasses,statefulsets -n <ns> <name> -o yaml`
- **Flag when:** A stateful workload using PersistentVolumes references a ComputeClass whose `priorities[]` mixes Gen 2 VMs (`n2`, `n2d`, `c2`) and Gen 4/Hyperdisk VMs (`c4`, `n4`, `c3`), causing PV attachment deadlocks upon failover.
- **Do NOT flag:** Stateless workloads; ComputeClasses whose priorities are purely Gen 2 or purely Gen 4/Hyperdisk-compatible; clusters running GKE 1.35.3+ using the `dynamic-rwo` StorageClass.
- **Severity:** `critical`.
- **Impact:** "Stateful PV workload mixes Gen 2 and Gen 4 machine families, causing volume attachment failures and deadlocks when scaling across nodes."
- **Remediation:** On GKE 1.35.3+, emit `kind: manifest` updating the StorageClass to `dynamic-rwo` (which makes autoscaler disk-topology aware). On older versions or fixed disks, emit `kind: manual` to unify priorities.

#### 3.6 Incompatible machine families for Hyperdisk workloads (`ccc-hyperdisk-incompatible`)

- **Reference:** `skills/gke-compute-classes/references/compute-class-gotchas-and-cuds.md`, `skills/gke-compute-classes/references/compute-class-provisioning-methods.md`
- **Command:** `kubectl --context <ctx> get storageclasses,computeclasses,deployments -n <ns> -o yaml`
- **Flag when:** A workload using Hyperdisk storage (`hyperdisk-balanced`, `hyperdisk-throughput`, `hyperdisk-extreme`) uses a ComputeClass that falls back to older generation machine families (`c2`, `n2`, `e2`) that do not support Hyperdisk CSI drivers.
- **Do NOT flag:** Workloads using standard Persistent Disk (`pd-standard`, `pd-ssd`); ComputeClasses falling back only to Hyperdisk-capable families (`c3`, `c4`, `n4`, `c3d`).
- **Severity:** `critical`.
- **Impact:** "Autoscaler fallback lands on an older machine family (c2/n2/e2) that does not support Hyperdisk, causing node provisioning or pod volume attachment to fail."
- **Remediation:** `kind: manifest`. Update ComputeClass fallback priorities to Hyperdisk-compatible families (`c3`, `c4`, `n4`) and remove incompatible older generations.

#### 3.7 Regional quota exhaustion risk across fleet (`quota-exhaustion-risk`)

- **Reference:** `skills/gke-compute-classes/references/compute-class-gotchas-and-cuds.md`
- **Command:** `gcloud compute regions describe <region> --project=<project> --format="json(quotas)"`
- **Flag when:** a GPU, TPU or CPU quota's `usage` in a project's region reaches >=90% of its `limit` (e.g. 22 of 24 L4 GPUs in use). One finding per region and metric, object `Quota/<region>:<metric>`.
- **Do NOT flag:** a quota whose `usage` is under 90% of its `limit`, however much demand the fleet's workloads could add to it; a `COMMITTED_*` metric at any usage, because it limits how much capacity committed-use discounts can buy, not how many nodes the autoscaler can run.
- **Severity:** `critical`.
- **Impact:** "`<metric>` quota in `<region>` is at `<usage>` of `<limit>`; once it is reached, Cluster Autoscaler cannot provision additional nodes there even if physical capacity exists."
- **Remediation:** `kind: manifest`. Adjust workload request caps in GitOps manifests to fit strictly within quota limits, and submit a quota increase recommendation for the GCP project.

#### 3.8 High preemption risk or low obtainability on Spot instances (`spot-scarcity-risk`)

- **Reference:** `skills/gke-compute-classes/references/compute-class-prioritization.md`
- **Command:** run by the §3 collector — `gcloud beta compute advice capacity-history --region=<region> --machine-type=<machine-type> --provisioning-model=SPOT --types=PREEMPTION,PRICE --project=<project> --format=json`, once per Spot machine shape the fleet requests. `--machine-type` is singular and required, as are `--provisioning-model` and `--types`; the plural `--instance-selection-machine-types`/`--size` spelling belongs to the sibling `gcloud beta compute advice capacity` and this command rejects it.
- **Flag when:** Workloads or ComputeClasses request Spot VM shapes that have high historical preemption rates (>20%) or low obtainability scores in `compute advice`, without alternative family fallbacks. The collector reads the **mean** of the daily `preemptionRate` values, over at least seven of them — one bad afternoon inside a calm month is a zonal incident that already resolved, and a shape with less history than that is reported as unmeasured rather than clean.
- **Do NOT flag:** Spot configurations that have high obtainability scores or comprehensive multi-family fallbacks; non-production environments.
- **Severity:** `major`.
- **Impact:** "Spot machine shapes have high historical preemption rates and severe obtainability constraints, putting workload uptime at extreme risk."
- **Remediation:** `kind: manifest`. Expand instance selection to include lower-preemption machine types and add secondary on-demand fallback priorities in GitOps.

#### 3.9 Single-zone node pool, or one at its autoscaling ceiling (`single-zone-nodepool`)

- **Reference:** `skills/gke-compute-classes/references/compute-class-provisioning-methods.md`
- **Command:** `gcloud container node-pools list --cluster=<cluster> --location=<location> --project=<project> --format=json`
- **Flag when:** A Standard mode GKE cluster has autoscaling node pools restricted to a single zone with no Node Auto-Provisioning (NAP) and no untainted zonal pool's fallback — an untainted multi-zone node pool of the same machine type (a tainted zonal pool is flagged regardless; the excerpt says which test failed), or a node pool's live node count is `>= 90%` of its **effective** ceiling — that ceiling is a hard stop, not a soft one, so "close to it" means measurably close, not a judgment call. Effective, because `autoscaling.maxNodeCount` is a _per-location_ limit ("maximum number of nodes for one location in the NodePool", in the API's words) while the live count is a pool total summed over every zone: the pool-wide ceiling is `maxNodeCount` times the zones the pool spans, unless the pool sets the mutually-exclusive `totalMaxNodeCount`, which is already pool-wide. The collector computes this and names the basis in the excerpt.
- **Do NOT flag:** Autopilot clusters (fully managed multi-zone); an untainted single-zone pool beside an untainted multi-zone pool of the same machine type on the same cluster; a tainted multi-zone pool is no fallback, since the zonal pool's pods do not tolerate its taints.
- **Severity:** `major`.
- **Impact:** two unrelated conditions share this check and no one sentence is true of both, so the collector writes the arm's own Impact onto the finding and marks it authoritative — `finish` restores it over anything published in its place, so rewriting one costs the rewrite, not the finding. State it as given rather than re-deriving it. **The two arms are not exclusive**, and a pool matching both gets both excerpt clauses and both sentences, joined. What it writes:
  - `single-zone` in the excerpt — the zone-locked arm, naming the stockout, the pods pinned to that zone by a nodeSelector or a zonal-disk PersistentVolume, and per-node-group backoff. Do not widen that to "halts all cluster auto-scaling": GKE's autoscaler treats each pool-zone pair as its own node group and backs off only the one that failed. The sentence carries the one documented exception — 45% of a cluster's nodes unready halts every operation — so do not add it a second time.
  - `at its autoscaling ceiling` — **not** the zone-locked arm on its own. It lists the zones the pool spans, frequently more than one, and every zonal-stockout sentence is false of it. The sentence states the **headroom**, not a stop: cluster autoscaler skips a node group only on `currentTargetSize >= MaxSize`, so at 27 of 30 the next scale-up adds three more nodes. It carries the caveat that the count is of live Nodes while the autoscaler compares target size, a NAP clause where the cluster can provision a different pool instead, and the reason the ceiling is not itself a supply signal. A scale event reaching a configured limit is the autoscaler working; do not call it a capacity failure. Equally, do not call it purely configuration — a pool can reach its ceiling because backoff in a sibling zone pushed the whole scale-up delta here, or because regional quota binds below the field.
- **Remediation:** `kind: manifest`, and the same arm decides the change. Zone-locked: propose enabling multi-zone node pools or configuring Node Auto-Provisioning (NAP) in the Terraform/Kustomize declaration. At the ceiling: propose raising **whichever ceiling field the excerpt names** — `autoscaling.totalMaxNodeCount` when it says `totalMaxNodeCount`, `autoscaling.maxNodeCount` when it says `maxNodeCount`. The two are mutually exclusive at the API (`maxNodeCount` "cannot be used with total limits"), so proposing the wrong one is a manifest GKE rejects. Never propose multi-zone for the ceiling arm alone — the excerpt's zone list usually shows the operator already has it.
  - Adding zones to a pool that sizes itself with `minNodeCount`/`maxNodeCount` **multiplies both**, because both are per location: a 1–2 pool given two more zones becomes a 3–6 pool and GKE provisions the new per-zone minimum immediately. Say so in `recommendation.risk`, and propose `totalMinNodeCount`/`totalMaxNodeCount` instead when the existing floor is the one the operator wants to keep.
  - **A pool matching both arms takes one change, not two.** Adding zones multiplies the per-location ceiling by the zone count, so spreading a single-zone 1–2 pool over three zones raises its effective ceiling from 2 to 6 and closes the ceiling arm as a side effect. Propose the zone spread and say in `recommendation.risk` what it does to both the floor and the ceiling; propose a raised ceiling on its own only when the operator has said they want to stay in one zone.
  - **Switching an existing pool from the per-location fields to the pool-wide ones needs two API calls, so it is not a single apply.** GKE rejects any update carrying both — `node_pool_autoscaling.max_node_count and node_pool_autoscaling.total_max_node_count cannot be both greater than zero` — and dropping `maxNodeCount` from the manifest does not clear it from the live pool, so the one update the controller sends still carries the old per-location value beside the new total. The manifest is a correct destination that cannot be reached from where the pool is. This is not theoretical: a merged pull request made exactly this change to a Spot pool and left Config Connector in `UpdateFailed` with the cloud resource untouched for twenty minutes on 2026-09-05. Nothing in the audit noticed, because the audit's job ends when the pull request merges.
    - So when the pool's live autoscaling uses `minNodeCount`/`maxNodeCount` and your change needs `totalMinNodeCount`/`totalMaxNodeCount` — which it does whenever adding zones would multiply a floor the operator wants to keep — propose the manifest, and put the prerequisite in `recommendation.risk` as a command the operator runs once before merging: `gcloud container node-pools update <pool> --cluster=<cluster> --location=<location> --no-enable-autoscaling`. Say plainly that the pull request will not reconcile until they have. A `risk` that omits it describes a change that silently does not happen.
    - A pool already sizing itself with `total*` has no such prerequisite; adding zones to it is a clean single apply. Read which pair the pool actually uses before deciding, rather than which pair the manifest happens to show.

#### 3.10 Reservation bypass, unreachable zones, or unallocated capacity mismatch (`reservation-mismatch-risk`)

- **Reference:** `skills/gke-compute-classes/references/compute-class-gotchas-and-cuds.md`, `skills/gke-compute-classes/references/compute-class-debug.md`
- **Command:** `gcloud compute reservations list --project=<project> --format=json`
- **Flag when:** (a) A ComputeClass sets `reservations.affinity: AnyBestEffort/Automatic`, which silently bypasses ComputeClass priority chains and falls back to On-Demand at GCE layer; (b) A ComputeClass targets a specific reservation that does not exist or sits in an unreachable zone; or (c) `inUseCount / count <= 0.5` **and** `count - inUseCount >= 4` — at most half the reservation is in use, and at least four whole instances of headroom sit idle, while production workloads in the same region run unreserved. Both conditions together, not either alone: the ratio catches a large reservation nobody uses, the absolute floor keeps a tiny reservation's normal one-or-two-instance headroom from reading as a leak. (Note: CUDs are financial commitments, not physical capacity reservations).
- **Do NOT flag:** ComputeClasses with valid targeted reservation bindings; non-production workloads.
- **Severity:** `critical` for broken/bypassed bindings, `major` for unallocated capacity mismatches.
- **Impact:** "ComputeClass fallback priorities are rendered inert by Automatic reservation affinity, or expensive guaranteed reservation capacity sits idle during stockouts."
- **Remediation:** `kind: manifest`. Target specific reservation names (the part of a `Reservation/<zone>:<name>` object after the colon, never the whole object) in GitOps manifests or update ComputeClass location to align with active reservations.

#### 3.11 Autoscaler out-of-resources leading indicators (`autoscaler-out-of-resources`)

- **Reference:** `skills/gke-compute-classes/references/compute-class-debug.md`
- **Command:** run by the §3 collector — `gcloud logging read 'log_id("container.googleapis.com/cluster-autoscaler-visibility") AND resource.labels.cluster_name="<cluster>" AND resource.labels.location="<location>" AND (jsonPayload.noDecisionStatus.noScaleUp:* OR jsonPayload.resultInfo.results.errorMsg:*)' --project=<project> --freshness=24h --limit=1000 --format=json`. JSON rather than the `value(...)` projection: a stockout is written under **two** schemas, and that projection reads only one of them. `jsonPayload.resultInfo.results[].errorMsg` is a scale-up that was attempted and failed, carrying the affected instance group in `parameters[0]`; `jsonPayload.noDecisionStatus.noScaleUp.unhandledPodGroups[].napFailureReasons[]` is the node-auto-provisioning side, which never gets as far as an attempt.
- **Flag when:** Cluster autoscaler visibility logs emit `scale.up.error.out.of.resources`, `scale.up.error.quota.exceeded`, or `scale.up.error.ip.space.exhausted` within the past 24 hours, indicating that scale-up attempts failed before fallback recovery. One finding per distinct message id, not per log entry: a wedged cluster emits the same id every autoscaler tick and the remediation below branches on the id.
- **Do NOT flag:** Clusters with clean autoscaler visibility logs over 24h.
- **Severity:** `critical`.
- **Impact:** "Autoscaler has actively failed scale-up attempts due to physical cloud stockouts, quota exhaustion, or pod subnet IP exhaustion."
- **Remediation:**
  - `scale.up.error.out.of.resources`: `kind: manifest`. Where a `ComputeClass` manifest for the affected workload already exists in the GitOps repo, add secondary fallback machine families and multi-zone support to it. Where none exists — a default Autopilot workload, say — write a **new** ComputeClass declaration under §4's declaration rule and point the affected workload at it, so the class is under declarative control rather than a snippet a human retypes. That is two edits, the class file and the workload that selects it, and a finding carries one `remediation.path`: set it to the class file. The collector marks this case `needs_triage: new-computeclass` (and any finding it cannot place), so the automatic sweep skips it; a human asks for it with `/remediate`, and because the harness stages only the finding's one path, that pull request holds the class alone and the selector is added to it by hand — name the workload and the selector in `recommendation.risk`, with what the new class does to its placement. Verify the families against the §4 feasibility gate. The single case that stays `kind: manual` is a cluster §4's sync precondition rules out, where no file this audit writes would ever be applied.
  - `scale.up.error.quota.exceeded`: `kind: manual` (request a regional/family GCP compute quota increase via Google Cloud Console or `gcloud compute project-info describe`).
  - `scale.up.error.ip.space.exhausted`: `kind: manual` (expand the VPC pod subnet secondary CIDR range).

#### 3.12 Dangling, unlabelled, or invalid ComputeClass configurations (`dangling-compute-class`)

- **Reference:** `skills/gke-compute-classes/references/compute-class-crd-fields.md`, `skills/gke-compute-classes/references/compute-class-debug.md`
- **Command:** `kubectl --context <ctx> get computeclasses,deployments,statefulsets -A -o yaml`
- **Flag when:** (a) A workload's `nodeSelector: cloud.google.com/compute-class` or namespace `cloud.google.com/default-compute-class` references a ComputeClass that does not exist; (b) A ComputeClass `status.conditions` reports invalid configuration; (c) `nodePoolAutoCreation.enabled` is false and referenced node pools lack `cloud.google.com/compute-class` label/taints; or (d) A GPU workload references a ComputeClass without declaring `nvidia.com/gpu` tolerations.
- **Do NOT flag:** Workloads referencing valid, reconciled ComputeClasses with matching node pool labels and tolerations; workloads selecting a GKE built-in compute class by its exact case, which GKE provides without a `ComputeClass` object in the dump — on Autopilot any of `Balanced`, `Scale-Out`, `Performance`, `Accelerator`, `autopilot`, `autopilot-spot`, `autopilot-arm`; on Standard only `autopilot` and `autopilot-spot`.
- **Severity:** `critical`.
- **Impact:** "Workload cannot be scheduled due to dangling class references, invalid CRD configuration, or missing node tolerations, causing permanent Pending state."
- **Remediation:** `kind: manifest`. Correct the ComputeClass name in GitOps, fix invalid CRD fields, or add required GPU tolerations to workload templates.

### 4. Generate remediation artifacts

- Locate the existing declaration in the GitOps clone (`grep -rl "name: <object>" --include='*.yaml' <workspace>`).
- **This audit's declaration rule is the fleet-audit skill's "A `path` is discovered, never invented" rule (`SKILL.md`, under "The findings document")**: it decides where the file goes, for an object the repo already declares and for one it does not yet. Read it there; this audit adds only what follows. Unlike most streams, this one has a real create case — a default Autopilot workload hitting `scale.up.error.out.of.resources` needs a ComputeClass that does not exist anywhere yet — so the new-object branch is the one you will reach most often, and it is a `manifest`, not a `manual` — one the sweep skips, since its path is the class and the workload's selector is a second edit (§3.11). The sibling that proves the directory is reconciled is another Kubernetes object declared here and running on the same cluster; a new ComputeClass goes beside it, named after the class. Say in `recommendation.rationale` which branch you took and how you established it.
- **Remediation Feasibility Gate**: Before proposing a new machine family or Spot tier, verify that:
  1. The target GCP zone actually offers the machine type (`gcloud compute machine-types list --zones=<zone>`).
  2. For On-Demand proposals, the project quota for that family (`N4_CPUS`, `C4_CPUS`, GPU types) is greater than 0 (`gcloud compute regions describe <region> --project=<project>`).
  3. For Spot proposals, the project has room for the Spot CPUs. Read `PREEMPTIBLE_CPUS` from `gcloud compute regions describe <region>`, but **a limit of 0 does not mean infeasible.** Preemptible quota is granted, not enforced by default: Compute Engine hides the metric until it grants it, and the legacy API renders that absent limit as `0.0`. Until it is granted, Spot VMs consume the ordinary `CPUS` quota, so compare the proposal against `CPUS`. Treating `PREEMPTIBLE_CPUS: 0` as a blocker fails a proposal the project can plainly satisfy: a project with no preemptible grant reports `PREEMPTIBLE_CPUS limit=0 usage=0` in `us-east4` while a Spot `e2-standard-4` runs in it. Once the quota **is** granted, the fallback closes — Spot consumes only preemptible quota and cannot revert to `CPUS` — so a limit greater than 0 is the real ceiling and `CPUS` stops being the number to check.
     - The grant is per **metric**, not per region: the same project and region reports `preemptible_nvidia_t4_gpus` at 4 and `preemptible_nvidia_a100_gpus` at 16 while `preemptible_cpus` is ungranted. Read the metric you are about to consume.
     - A deliberate override of `0` renders identically to an absent limit in `gcloud compute regions describe`, and that one **is** a blocker. To tell them apart, read Service Usage instead: `GET .../consumerQuotaMetrics/compute.googleapis.com%2Fpreemptible_cpus` — no `effectiveLimit` and no overrides is ungranted, `effectiveLimit: 0` beside a populated override is a cap.
     - `PREEMPTIBLE_LOCAL_SSD_GB` follows the same grant rule, but its fallback target is `LOCAL_SSD_TOTAL_GB`, not `CPUS`.
- Edit the manifest directly in `<workspace>`, adding the necessary fallback machine families, zones, or quota adjustments.
- **Mandatory Remediation Comments**: For every modified line in YAML, append an inline `# Remediation: <reason>` comment.
- Set `remediation.path` to the repo-relative file path, with `kind: manifest`.
- `finish` opens a pull request unasked for a manifest finding graded `critical`, or `major` on `ccc-no-ondemand-floor` where the collector's candidate is at least `major`, unless the collector marked it `needs_triage` — so a missing On-Demand floor (at `major`, or `critical` for an inference workload) arrives as a pull request where the repo declares the class, and §3.11's new-ComputeClass fix, which the collector marks `needs_triage: new-computeclass`, does not. Every other `major` manifest here — a smaller replica shape, a Spot fallback, a raised zonal ceiling — waits for `/remediate`: each changes what the workload costs or where it runs.
- Reviewers may comment `/remediate <finding-id>` or `/remediate all` on the ledger issue to promote findings into PRs.

### 5. Emit findings.json

Write the schema exactly as the helper validates it to the `findings_path` returned in Step 0: `audit` set to `stockout-prevention`; `scope.clusters` non-empty, each entry carrying the mandatory `checks_run` list of `{check, command}` objects for the §3 checks that actually ran there; and for each finding, `check`, `severity`, `title`, `cluster`, `namespace`, `object`, `evidence.command`, `evidence.excerpt`, `impact`, `recommendation`, and `remediation`.

### 6. Close the audit run

```bash
python3 ./skills/fleet-audit/scripts/audit_report.py finish --audit stockout-prevention \
  --findings-file /opt/data/scratch/findings_stockout-prevention.json \
  --manifest-file /opt/data/scratch/manifest_stockout-prevention.json \
  [--repo "<owner>/<repo>"]
```

Always pass `--manifest-file`: nothing else checks the document against what the collector actually ran. On a run where §3's collector never produced one — it crashed, or the fleet was unreachable, and every check on every cluster came from the manual fallback — pass `--no-collector-manifest '<why>'` instead; it publishes but reports the reason as a coverage gap, so the run is partial. Given a manifest, `finish` rejects a `checks_run` entry on a `"collected"` cluster that names a check the manifest never recorded at `rc == 0`, and rejects a `"collected"` cluster the document leaves out of `scope.clusters` altogether.

One JSON line comes back, carrying `status`, `issue_url`, `new`, `resolved`, `prs_opened`, `prs_closed`, `partial`, `coverage_gaps`, and `silent_ok`. Exit 2 means the validator rejected the document and nothing was published — fix the document, do not retry blind. The exception is a `BROKER UNAVAILABLE` line: the document was fine and the broker went away, possibly after the ledger was already rewritten, so do not report that nothing was published; re-run `finish` once the broker answers (`skills/fleet-audit/SKILL.md`, step 3). Exit 1 is fatal. Exit 0 means it published.

`partial` is `true` when the run could not read the whole fleet: any cluster in `scope.skipped`, or any cluster kept in scope with a `limitations` note. `coverage_gaps` names each one in a sentence. The harness then refuses to draw conclusions from silence, because a workload or ComputeClass you never queried is not one that got resolved: `resolved` comes back `0` and no resolved-delta is posted, no remediation PR is retired as stale, and the ledger issue stays open even at zero findings — `status` is still `CLEAN`, but the issue survives with a comment naming what went unread. A check declared in `checks_not_applicable` is not a gap and does not raise the flag; it left the denominator. Nothing else raises it — it is `true` if and only if `coverage_gaps` is non-empty. A fleet big enough that the description had to drop findings is not a coverage gap: those workloads were queried, the title counts them, and the body says which ones it left out. A run that audited no cluster is partial too unless every `project/<id>` target carries `clusters_listed: 0`: a project target without it may hold clusters nobody read.

**`silent_ok` decides silence. Do not re-derive it.** `finish` returns `silent_ok: true` only when this run moved nothing an operator needs to hear about: nothing new, nothing resolved, no coverage gap, no remediation PR opened or closed. Read the flag rather than reassembling that from `status`, `new`, `resolved`, and `partial` yourself — that arithmetic is where a run talks itself into silence it has not earned. Two rules, and they are the whole rule:

- On a **scheduled** run, `silent_ok: true` → your entire final response is exactly `[SILENT]`. Otherwise report, and every report carries `issue_url` in full.
- **An on-demand run is never silent.** If a person dispatched this job — from a kanban card or straight from chat — someone is waiting on the answer, and `[SILENT]` throws it away. Report the outcome and the ledger URL whatever `silent_ok` says.

What to report in each case:

- `silent_ok: true` — `[SILENT]` on a scheduled run, nothing else and no preamble. On `CLEAN` the ledger issue closed as completed and every open remediation PR for this stream closed with it; on `UPDATED` the ledger was rewritten but nothing moved. Dispatched on demand, say which in one line and give the issue URL.
- `status: "CLEAN"` with `resolved: > 0` — every capacity gap this ledger tracked has been closed. Report the issue URL and the count.
- `status: "CLEAN"` with `partial: true` — nothing reproduced, but the ledger and its PRs stayed open because the coverage was incomplete. One line, the clean result plus the `coverage_gaps`, then stop.
- `status: "HELD"` — zero findings, but the ledger stayed open because the run did not account for findings it was carrying (`start` listed them under `carried`; `unaccounted` names the ones held): one line reporting the clean result, the held ids and the issue URL, then stop. On the next run, report each one, or list it under `resolved_because` if you re-ran its check and saw it gone.
- Any other outcome — reply with **one line**: counts by severity, new vs. resolved, skipped-cluster count if any, remediation PRs opened or closed, and the `issue_url`.

## Red Lines

- **Read-only against every cluster.** No `apply`, `patch`, `edit`, `delete`, `scale`, `drain`, `cordon`, or eviction.
- **No hand-written issue or PR bodies, and no direct git or forge calls.** `audit_report.py` owns the ledger issue, the remediation branches, the commits, and every body it renders.
- **No credentials in evidence.** A Secret's `data:` block, a token, or a private key never enters an excerpt; re-read with a projection that omits it.
- **A finding you cannot reproduce is dropped, not softened.** `evidence.command` is the literal command you executed; if the confirm read fails or the condition has cleared, the finding does not ship.
- **No fabricated numbers.** Resource quantities and machine families are either read off the live object or left to a human.
- **Stable ids or the delta lies.** An unstable id — one that varies between runs because the `object` it is derived from moved — turns one persistent problem into an infinite stream of "new" findings.
