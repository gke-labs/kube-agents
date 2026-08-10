# SOP: Workload Reliability Audit (Daily Governance)

**Purpose:** Sweep every managed GKE cluster for workloads configured in a way that will hurt during a node drain, a control-plane or node-pool upgrade, a scale event, or a traffic spike. The question this audit answers for a platform admin is: _which workloads on my fleet break when I upgrade a node pool, and which ones cannot scale?_ Output is this stream's single GitHub ledger issue, rewritten in place on every run, plus narrow remediation Pull Requests carrying generated manifests for the findings that get promoted.

**Cron:** id `obtainability-audit`, schedule `50 6 * * *` (daily 06:50 UTC). The id is a stable observability identifier and does not change even though the audit is named "Workload Reliability".

**Data sources:** `kubectl` read verbs, `gcloud container ...`, the `gke` MCP server, and the Config Controller MCP tools (`list_cc_pods`, `get_cc_pod_diagnostics`, `list_cc_healthchecks`, `get_cc_operator_status`). **Nothing else** — no BigQuery, no Prometheus/GMP, no VPA recommendations, no Policy Controller, no external blueprint, no delegation to Cluster Agents via kanban. Every conclusion is derived from live cluster reads you performed in this run.

---

## Execution Checklist

### 0. Open the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py start --audit obtainability-audit
```

Returns `{"issue": <int|null>, "repo":"org/repo", "workspace":"/opt/data/gitops/obtainability-audit/org__repo", "findings_path":"/opt/data/scratch/findings_obtainability-audit.json", "pending_remediation_requests":[…]}`. Keep `findings_path` and `workspace` from this call; you write into both.

- `workspace` is the GitOps clone `start` made for you. The audit pod does not begin life inside a checkout, so this is the only tree that exists, and every `remediation.path` in Step 4 is resolved against it — a manifest written elsewhere is one the harness cannot find.
- `issue` is this stream's open ledger issue, or `null` when it has none. Either way you never create it — `finish` owns that.
- `pending_remediation_requests` lists finding ids a repo writer asked for with a `/remediate` comment on the ledger. Write a manifest for each one while you inspect (Step 4), or the promotion fails for want of a file. Each comment is answered once and only once — an acknowledgement listing every target and its outcome, or a single refusal with the reason (no write access, an id absent from the current document, a target that is not a `manifest`, or a command not written at the start of its own line and so never parsed as one) — with a hidden marker keeping yesterday's request from being answered again today.
- `start` creates and resets no branch. There is no report branch.

The helper owns every `git`/`gh` operation and renders the ledger issue body and every remediation PR body — **never hand-write an issue or PR body, never run `git commit`, `git push`, `gh issue create`, `gh pr create`, or `gh issue comment` yourself.**

**Never comment on the ledger yourself.** `/remediate` is a human reviewer's instruction to this harness, not a step in the audit: an agent that posts it — including when someone asks for a fix in chat — is authorizing its own pull request. `finish` ignores a `/remediate` from a machine account, so posting one achieves nothing but noise on the issue.

### 1. Enumerate the target fleet

```bash
gcloud container clusters list --format=json
```

- Target every cluster with `status == "RUNNING"`. Record `{name, location, project, checks_run}` into `scope.clusters`. Note each cluster's `autopilot.enabled` — Step 3 changes behaviour on Autopilot. Carry that in each affected finding's `impact` (§3.1 and §3.2 are the two it moves), and surface it in `evidence.excerpt` where it changes a verdict. **Not in `limitations`:** every non-empty `limitations` string is read as a coverage gap, and a fleet with one Autopilot cluster would then publish `partial: true` on every run it ever makes, with the ledger permanently unclosable. Autopilot changes how severe a finding is, not how much of the cluster you saw.
- **`checks_run` is mandatory on every cluster,** and each entry is an object, never a bare string:

  ```json
  {
    "check": "no-pdb",
    "command": "kubectl --context prod-usc1 get pdb -A -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name --no-headers"
  }
  ```

  `check` is the backticked slug from the §3 heading that defines it — `no-requests`, `no-pdb`, and so on — never the section number and never prose. (`start` prints the full roster of eleven; the SOP still says what each check _is_.) `command` is the literal invocation you issued on that cluster for that check, with its `--context` and the namespace or resource it targeted. It must name one of `kubectl`, `gcloud`, `gsutil`, `bq`, `helm`, or `curl`; `echo`, `cat`, `python3 -c`, and a call back into `audit_report.py` are all rejected, as is anything under eight characters.

  The validator rejects an unknown slug, a duplicate, a missing or unusable command, the field being absent, and an empty list unless that cluster's `limitations` says why nothing ran: a cluster you could read but ran nothing against is not a clean cluster, it is an audit that did not happen. Anything short of the checks that apply to that cluster makes the run **partial** exactly as a `limitations` note does, so the ledger stays open and nothing is announced as resolved. Append the entry when its check completes, not when you intend to run it, and paste the command rather than reconstructing it — every one is published verbatim in the ledger under _How this run checked the fleet_.

- **A check the cluster's shape rules out is not a gap — declare it.** Alongside `checks_run`, a cluster may carry `checks_not_applicable` as a list of `{check, reason}`:

  ```json
  {
    "check": "no-hpa",
    "reason": "autoscaling/v2 is not served by this cluster's API server; an HPA cannot exist here."
  }
  ```

  Same slugs as `checks_run`, and the `reason` must say why the check _cannot_ apply here — "N/A" and "not applicable" are rejected; name the property of the cluster that rules it out. Those checks leave the denominator instead of counting as missing, so the cluster reads as complete at ten of ten rather than forever-incomplete at ten of eleven, and the ledger can eventually close. **Expect to use this rarely on this audit.** Every §3 check is workload-shaped and applies wherever workloads run, and Autopilot in particular does **not** make any of them inapplicable — it only downgrades 3.1 and 3.2 by one severity, which belongs in those findings' `impact` and in neither of these two lists. A check you could have run and did not is a `limitations` note and a real gap. The validator rejects a slug in both lists, a duplicate, an unknown slug, and a reason under sixteen characters, but it cannot tell an honest exclusion from a convenient one — that part is on you.

- **One question decides the scope list.** A cluster appears in exactly one scope list. Could you read it? Yes → `scope.clusters`; name any check that could have run there and did not in that cluster's `limitations`, and any check its shape rules out in `checks_not_applicable`. No → `scope.skipped`. Nothing goes in both, and nothing in `scope.skipped` may appear in a finding. The validator enforces both halves: it rejects a document whose two lists overlap, and any finding whose `cluster` names a `scope.skipped` entry.
- A cluster you cannot read goes in `scope.skipped` with a literal reason: `"status=STOPPING"`, `"get-credentials failed: <stderr first line>"`, `"timeout after 30s"`. A skipped cluster is never silently dropped.
- A partial read is **not** a skip. If the dump succeeded but one kind was refused, the cluster stays in `scope.clusters` and the refusal goes in its `limitations`: `"RBAC: cannot list horizontalpodautoscalers; checks 3.5 and 3.6 not run."` Skipping it would suppress every finding you did prove there.
- Obtain per-cluster credentials into an isolated kubeconfig so clusters cannot bleed into each other:
  ```bash
  export KC="${HERMES_HOME:-/opt/data}/.kubeconfigs/kubeconfig_<project>_<cluster>_<location>.yaml"
  KUBECONFIG=$KC gcloud container clusters get-credentials <cluster> --location=<location> --project=<project>
  ```
- If **zero** clusters land in `scope.clusters`, do **not** call `finish` — the helper hard-fails on an empty scope. Report the enumeration failure as your one-line summary and stop.

### 2. Collect workload state

One JSON dump per cluster answers every check in Step 3. **Do not run a separate full-fleet query per check.**

```bash
KUBECONFIG=$KC kubectl get deployments,statefulsets,daemonsets,poddisruptionbudgets,\
horizontalpodautoscalers,services,limitranges -A -o json > /opt/data/scratch/wra_state_<cluster>.json
```

- Because multiple kinds are requested, every element of `.items[]` carries its own `kind` — filter with `select(.kind=="…")`. (A single-kind `kubectl get` omits per-item `kind`; do not build the checks on that shape.)
- Read workload **templates** (`spec.template.spec`), not live Pods. Templates are what an admin edits, and they are unaffected by admission-time defaulting.
- Pods, Jobs, CronJobs, and Events are deliberately excluded: Events expire in roughly an hour, so a fixed 06:50 run samples an arbitrary window, and pod-level data doubles the payload without changing any verdict.

**Autopilot adjustments.** Autopilot injects resource requests (and, absent explicit limits, mirrors limits from requests) at Pod admission, so a missing-request or missing-memory-limit template is a cost-attribution and predictability problem there, not a scheduling failure. On an Autopilot cluster: downgrade checks 3.1 and 3.2 by one severity level and say so in `impact`, naming the mode there. That is the only place the mode is recorded. Autopilot is never a skip — you read the cluster and every check ran — and it is not a `limitations` note either: that field is the coverage flag, and a mode note parked in it would mark a fully audited cluster as partially audited for as long as the cluster exists. Hostname pinning (3.7) stays `critical` on Autopilot — nodes are ephemeral and are replaced on every upgrade, so a hostname-pinned pod has a guaranteed outage. All other checks are mode-independent.

### 3. Checks

**Standard exclusions — apply to every check below.** Skip an object if any holds:

- **S1 — system namespace:** `kube-system`, `kube-public`, `kube-node-lease`, `gmp-system`, `gmp-public`, `gke-gmp-system`, `cnrm-system`, `configconnector-operator-system`, `krmapihosting-system`, `istio-system`, `asm-system`, `anthos-identity-service`, `gatekeeper-system`, `composer-system`, or any namespace matching `gke-*`, `gke-managed-*`, or `config-management-*`. This is the fleet-wide system-namespace set, spelled the same way in the Security & RBAC Posture Audit (`$SYS`) and the Fleet Waste Audit (`SYSTEM_NS`); `kubeagents-system` is deliberately **not** in it, because the harness audits itself.
- **S2 — GKE-managed object:** carries the label `addonmanager.kubernetes.io/mode` (any value). GKE reverts edits to these; a finding is unactionable.
- **S3 — operator-owned:** the workload has a non-empty `metadata.ownerReferences` (its replica count, PDB, and probes belong to its controller, not to a human).
- **S4 — explicit opt-out:** the workload carries `kubeagents.x-k8s.io/reliability-audit: exempt` as a label or annotation.
- **S5 — not running:** `spec.replicas == 0`, or the workload is a Job/CronJob or is owned by one.

**Evidence discipline.** The dump is the _detector_; a live single-object read is the _confirmer_. For every candidate finding, run the object-scoped command below, capture a trimmed excerpt, and store that exact string in `evidence.command`. If the confirm command fails or the condition no longer holds, **drop the finding — do not soften it.**

```bash
KUBECONFIG=$KC kubectl get <kind> -n <ns> <name> -o yaml
```

**Never paste a credential into an excerpt.** A Secret's `data:` block, a token, a kubeconfig, or a private key — including one sitting in a container's `env` — must never reach `evidence.excerpt`. Re-run the confirm read with a field selector or an `-o jsonpath` that omits it (`-o jsonpath='{.spec.template.spec.containers[*].name}'`). The harness redacts high-confidence credential shapes as a backstop, not as the primary control.

If one check yields more than 25 findings in a single cluster, roll the surplus into one namespace-level finding per namespace: same severity, and a namespace-scoped confirm command (`kubectl get <kind> -n <ns> -o yaml`). Give the roll-up the scope it covers as its `object` — `Namespace/<ns>`, nothing more — so its identity is that scope; put the workload count in the `title` and the workload names in `evidence.excerpt`, never in `object`, because the count moves the moment a workload is added and an `object` that moves renames the finding. The individual findings it replaces are not also emitted, so nothing collides. The cap is about reviewer attention, not about a byte limit: the harness caps the rendered issue body at 60,000 characters, trims each excerpt to 40 lines / 2,000 characters and the scope tables to 60 rows, and on a large fleet drops findings from the description least-severe-end first and says in the body that it did — while the title's counts stay the true totals. A fleet that has never set requests still gets counted honestly; it just should not spend the whole body saying so.

**Finding identity.** **Do not write an `id`.** The harness derives it from `check`, `cluster`, `namespace` and `object`, and ignores any `id` in the file. `check` is the backticked slug in the check's heading below; a slug outside this SOP's roster is rejected.

Identity is only as stable as those four fields, so **never** let a timestamp, replica count, image tag, pod name, or resource value into `object` — audit the owning controller, never the pod, whose name carries a random suffix. A finding whose identity moves between runs is announced as fixed and re-announced as new, and the delta the ledger exists to produce becomes noise. Two findings agreeing on all four fields are one finding, and the harness refuses the document rather than collapsing them silently.

#### 3.1 No CPU or memory request (`no-requests`)

**This check owns the _absence_ of a request and nothing else.** The **value** of a request belongs to the Fleet Waste Audit (`fleet-wide-cost-analysis` check 3.1, `overrequest`), which has the usage samples this audit does not collect. Never propose a number here: two audits sizing the same container in opposite directions makes each run flag the state the other just created.

- **Command:** derived from `$STATE`; confirmed with the object-scoped read above.
- **Flag when:** any container in `spec.template.spec.containers[]`, or any native sidecar (`initContainers[]` with `restartPolicy: Always`, which counts toward the pod's effective request), declares **no** `resources.requests.cpu` or **no** `resources.requests.memory`.
- **Do NOT flag:** standard exclusions; plain init containers; any container whose namespace has a `LimitRange` with a matching `spec.limits[].defaultRequest` (the request is injected at admission, so there is nothing to fix); **a request that exists but looks wrong** — too large, too small, or copied from a template. Whether a declared request is the right size is the waste audit's question, not this one.
- **Severity:** `major` (`minor` on Autopilot). The scheduler treats an unrequested container as free, so this corrupts bin-packing for every other workload on the node, not just this one.
- **Impact:** "The scheduler and cluster autoscaler size this cluster as if this workload costs nothing; its pods are the first evicted under node pressure and its cost cannot be attributed."
- **Remediation:** `kind: manual`, always. Action: "Declare an explicit `resources.requests.cpu` and `resources.requests.memory` on this container." Note that the size comes from the Fleet Waste Audit's usage sampling or from the owner's own observation (`kubectl top pod -n <ns>`) over a representative window, and that this audit deliberately proposes no figure. **Never invent a request value, and never derive one from the container's limits** — a limit is a ceiling the owner chose, not a steady-state size.

#### 3.2 No memory limit (`no-memory-limit`)

- **Command:** derived from `$STATE`; confirmed with the object-scoped read.
- **Flag when:** a container has no `resources.limits.memory`.
- **Do NOT flag:** standard exclusions; namespaces whose `LimitRange` sets a `default.memory`; **missing CPU limits, ever.** Omitting a CPU limit is a deliberate and widely recommended choice — it avoids CFS throttling — and flagging it would make this audit noise.
- **Severity:** `major`. An unbounded container's blast radius is the whole node: a leak drives the kubelet to evict neighbouring pods before the offender is killed.
- **Impact:** "A memory leak here is absorbed by the node, not by this pod — the kubelet evicts co-located workloads first."
- **Remediation:** if `resources.requests.memory` is set, emit `kind: manifest` (subject to §4's declaration rule) setting `limits.memory` to that same declared request (Guaranteed QoS, no invented number), and state in `note` that this is the conservative reading of the owner's own request and needs owner sign-off. If no memory request exists either, the finding is already covered by 3.1 — emit `kind: manual` and cross-reference it.

#### 3.3 Multi-replica workload with no PodDisruptionBudget (`no-pdb`)

**This audit owns PodDisruptionBudgets** — both their absence (here) and their shape (3.4). The Fleet Waste Audit (`fleet-wide-cost-analysis` check 3.8) defers to these two and reports only the non-PDB reasons a node cannot be drained, so one PDB never carries two verdicts in two ledgers.

- **Command:** derived from `$STATE`; confirmed with the object-scoped read.
- **Flag when:** a Deployment or StatefulSet has `spec.replicas >= 2` and no PDB in the same namespace whose `spec.selector` matches `spec.template.metadata.labels`. Evaluate the selector properly (`matchLabels` **and** `matchExpressions`); never match on names.
- **Do NOT flag:** standard exclusions; **DaemonSets** (a drain deletes DaemonSet pods rather than rescheduling them — a PDB finding on a DaemonSet is nonsense and would be the fastest way to get this audit switched off); workloads with `spec.replicas <= 1`.
- **Severity:** `major`. The upgrade still completes; the workload is simply taken fully offline while it does.
- **Impact:** "Nothing constrains the eviction API, so a single node drain during an upgrade can terminate every replica at once."
- **Remediation:** `kind: manifest`. Generate a PDB with `maxUnavailable: 1` whose `spec.selector` **reproduces the workload's `spec.selector` exactly — every `matchLabels` entry and every `matchExpressions` term**, copied verbatim from the live object. Always `maxUnavailable`, **never** `minAvailable` — `minAvailable` is the shape that deadlocks drains (see 3.4), and `maxUnavailable: 1` is structurally safe at any replica count ≥ 2.
- **Never emit a PodDisruptionBudget whose `spec.selector` is empty or absent.** In `policy/v1` an empty selector matches **every pod in the namespace**, so the budget you meant for one Deployment governs evictions for all of them and can stall every drain there, node upgrades included. A workload selected purely by `matchExpressions` is the trap: copy only `matchLabels` and you emit `selector: {}`. If you cannot reproduce the workload's selector exactly — a term you cannot read, an operator you cannot represent — the finding degrades to `kind: manual` naming the selector the owner must supply. Guessing is not an option here.

#### 3.4 Drain-blocking PodDisruptionBudget (`blocking-pdb`)

- **Command:** derived from `$STATE`; confirmed with `kubectl get pdb -n <ns> <name> -o yaml`, whose `status` block is the corroborating excerpt.
- **Flag when:** a PDB has `maxUnavailable: 0` or `maxUnavailable: "0%"`; or `minAvailable` as an integer `>=` the matched workload's `spec.replicas`; or `minAvailable: "100%"`. Corroborate with `status.expectedPods > 0 && status.disruptionsAllowed == 0`, but decide on the spec — status alone is transient when a pod is briefly unready.
- **Do NOT flag:** standard exclusions (S1 applies to the PDB's namespace); a PDB whose `minAvailable` is genuinely below the target's replica count; a PDB matching a workload scaled to zero (`status.expectedPods == 0` — there is nothing to evict, so no drain is blocked); orphan PDBs matching no workload at all (harmless, and reported at most as a `minor` config-rot finding).
- **Severity:** `critical`. This is the highest-value finding in the audit and the one most often missed. It does not degrade a workload — it stops the cluster's entire lifecycle.
- **Impact:** "Blocks every node drain in this cluster indefinitely: node-pool upgrades, node auto-repair, and autoscaler scale-down all stall until a human deletes or edits this PDB."
- **Remediation:** if the matched workload has `spec.replicas >= 2`, emit `kind: manifest` (subject to §4's declaration rule) rewriting the PDB to `maxUnavailable: 1`, its `spec.selector` carried over from the live PDB unchanged — 3.3's empty-selector prohibition applies here too. If `replicas == 1` with `minAvailable: 1`, emit `kind: manual` — the PDB is doing exactly what it says, and the real fix (run more than one replica) is the owner's call, not a config change.

#### 3.5 Deployment with `replicas >= 3` and no HorizontalPodAutoscaler (`no-hpa`)

- **Command:** derived from `$STATE`; confirmed with the object-scoped read.
- **Flag when:** a Deployment has `spec.replicas >= 3` and no HPA in the namespace whose `spec.scaleTargetRef` resolves to `{apiVersion: apps/v1, kind: Deployment, name: <name>}`.
- **Do NOT flag:** standard exclusions; **StatefulSets** (horizontal autoscaling of stateful members is rarely safe and is an owner decision); DaemonSets; workloads already fronted by a KEDA-generated HPA — KEDA creates a real `HorizontalPodAutoscaler`, so the selector match above already covers them.
- **Severity:** `minor`. A fixed replica count is a capacity decision, not a fault; nothing breaks during an upgrade. Ranking it below 3.3 and 3.4 is the point.
- **Impact:** "Capacity is pinned at a hand-chosen number: the workload cannot absorb a traffic spike and cannot give capacity back when idle."
- **Remediation:** `kind: manual`. `minReplicas` can be taken from the observed count, but `maxReplicas` and the utilisation target cannot be derived from anything this audit can read — emit guidance ("set `minReplicas` to the current <n>; choose `maxReplicas` and the CPU target from the workload's own headroom requirements; a CPU request is a prerequisite for utilisation-based scaling") rather than a manifest full of invented numbers.

#### 3.6 HPA that cannot scale (`hpa-cannot-scale`)

- **Command:** derived from `$STATE`; confirmed with `kubectl get hpa -n <ns> <name> -o yaml`.
- **Flag when:** (a) `spec.minReplicas == spec.maxReplicas`; or (b) `spec.scaleTargetRef` names an object absent from the dump, corroborated by `status.conditions[type=AbleToScale].status == "False"`.
- **Do NOT flag:** standard exclusions; HPAs owned by a KEDA `ScaledObject` (S3 covers them — the real configuration lives in a CRD this audit does not read); HPAs whose target exists but is a kind outside the dump — the cluster was readable, so it stays in `scope.clusters`; note the unevaluated targets in that cluster's `limitations`, never as a finding.
- **Severity:** (a) `major` — the workload is pinned _and_ the admin believes it is autoscaled, so the HPA silently overrides the Deployment's own `replicas`. (b) `minor` — dangling config rot; nothing is currently degraded.
- **Impact:** (a) "An HPA is attached but `min == max`, so this workload cannot scale in either direction — the autoscaling is cosmetic." (b) "This HPA targets an object that no longer exists and autoscales nothing."
- **Remediation:** `kind: manual` for both. Widening a range and deleting a stale object are owner decisions, and this repo's manifest path cannot express a deletion.

#### 3.7 Rigid scheduling constraints (`rigid-scheduling`)

- **Command:** derived from `$STATE`; confirmed with the object-scoped read.
- **Flag when:** `spec.template.spec.nodeSelector` contains `kubernetes.io/hostname`; or `nodeSelector` pins `topology.kubernetes.io/zone` to one zone; or a `nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution` term restricts `kubernetes.io/hostname` or `topology.kubernetes.io/zone` to exactly one value.
- **Do NOT flag:** standard exclusions; zone terms listing two or more `values` (deliberate and healthy); `preferredDuringScheduling…` (soft, never blocks scheduling); hardware selectors — `cloud.google.com/gke-accelerator`, `cloud.google.com/machine-family`, `cloud.google.com/compute-class`, `cloud.google.com/gke-spot` — which are legitimate requirements, not rigidity; StatefulSets with `spec.volumeClaimTemplates` bound to zonal storage, which are _correctly_ zone-pinned by their disks.
- **Severity:** hostname pin → `critical` (with node auto-upgrade the node is guaranteed to be replaced, so this is a scheduled outage). Single-zone pin → `major`.
- **Impact:** hostname — "This pod cannot be rescheduled; the next node upgrade or repair takes it down and it does not come back." Zone — "Pinned to one zone: a zonal stockout or zonal outage takes this workload down while capacity sits unused in the other two."
- **Remediation:** `kind: manual` for both. A pin usually encodes an assumption (node-local state, a zonal disk, a licence) that this audit cannot see, and blindly stripping a scheduling constraint is how an audit causes an incident. Guidance: replace the pin with a compute class plus topology spread (see the `gke-compute-classes` skill).

#### 3.8 Multi-replica workload with no spreading (`no-spread`)

- **Command:** derived from `$STATE`; confirmed with the object-scoped read.
- **Flag when:** `spec.replicas >= 2` and `spec.template.spec.topologySpreadConstraints` is absent or empty **and** there is no `podAntiAffinity` (required or preferred) keyed on `kubernetes.io/hostname` or `topology.kubernetes.io/zone`.
- **Do NOT flag:** standard exclusions; DaemonSets (one pod per node by construction); workloads that already have either mechanism.
- **Severity:** `minor`. kube-scheduler applies best-effort default spreading, so co-location is possible rather than certain — but the default skew tolerance is wide enough that a small Deployment can still land entirely on one node.
- **Impact:** "Nothing guarantees these replicas are on different nodes; losing one node can take the whole workload out despite the replica count."
- **Remediation:** `kind: manifest` (subject to §4's declaration rule). Add a single `topologySpreadConstraints` entry: `maxSkew: 1`, `topologyKey: kubernetes.io/hostname`, `whenUnsatisfiable: ScheduleAnyway`, `labelSelector` reproducing the workload's own `spec.selector` in full — `matchLabels` and `matchExpressions` both, as in 3.3. `ScheduleAnyway` is mandatory here — `DoNotSchedule` can make a workload unschedulable, which is a worse outcome than the finding.

#### 3.9 Missing readiness probe (`probes-readiness`)

- **Command:** derived from `$STATE`; confirmed with the object-scoped read.
- **Flag when:** a container has no `readinessProbe` and the workload's pod labels are selected by a `Service` in the same namespace.
- **Do NOT flag:** standard exclusions; workloads no Service selects (nothing routes to them); Services of `type: ExternalName` or with no `selector`; injected sidecars that manage their own readiness (`istio-proxy`, `cloud-sql-proxy`, `gke-metadata-server`).
- **Severity:** `major`.
- **Impact:** "Every rollout sends production traffic to pods that are not yet serving, and a broken new version is never detected as broken."
- **Remediation:** `kind: manual`, always. A probe's path, port, and timings are application knowledge; a generated `/healthz` probe would break the workload the moment it was applied. **Do not generate probe YAML.**

#### 3.10 Missing liveness probe (`probes-liveness`)

- **Command:** derived from `$STATE`; confirmed with the object-scoped read.
- **Flag when:** a container has no `livenessProbe`.
- **Do NOT flag:** standard exclusions; injected sidecars that manage their own health (`istio-proxy`, `cloud-sql-proxy`, `gke-metadata-server`).
- **Severity:** `minor`, always. This check and 3.9 are deliberately separate and are **not** equivalent: a missing liveness probe is frequently the _correct_ choice — a badly tuned one causes restart storms — so it is reported as information, not as a defect. A workload missing both probes is one finding here and one under 3.9, never a single merged finding: they carry different severities, different impacts, and the harness derives a finding's identity from its check, so merging them would silently drop one.
- **Impact:** "A wedged process is never restarted automatically; recovery requires a human."
- **Remediation:** `kind: manual`, always. A probe's path, port, and timings are application knowledge; a generated `/healthz` probe would break the workload the moment it was applied. **Do not generate probe YAML.**

#### 3.11 Single-replica Service-backed Deployment (`single-replica`)

- **Command:** derived from `$STATE`; confirmed with the object-scoped read.
- **Flag when:** a Deployment has `spec.replicas == 1` and a `Service` in the namespace selects its pods.
- **Do NOT flag:** standard exclusions; StatefulSets (a single-member StatefulSet is usually intentional and often disk-bound); Deployments with `strategy.type: Recreate`, which explicitly declares that two copies must never run at once; workloads carrying the S4 opt-out label, which is the sanctioned way for an owner to say "this is meant to be one replica".
- **Severity:** `minor`. This is a known-cost design decision, not a misconfiguration.
- **Impact:** "Zero-downtime is impossible: every rollout, node drain, and node repair is a full outage for this service."
- **Remediation:** `kind: manual`. Going HA touches leader election, session handling, and storage — guidance only.

**Dropped deliberately.** Right-sizing from VPA recommendations, "HPA pegged at max", CPU-throttling ratios, and OOMKill history all require Prometheus/GMP, VPA, or an event history this audit is forbidden from or cannot sample reliably at a fixed daily time. Node-pool surge and maintenance-window settings are real reliability risks but belong to the upgrade/security-patch audit, not here.

### 4. Generate remediation artifacts

- **The declaration rule decides where the file goes, and both of its branches discover a directory that is already there.** A remediation that _changes an object that already exists_ — 3.2's and 3.8's workload edits, 3.4's PDB rewrite — must go to that object's **existing declaration in the GitOps repo**: locate it (`grep -rl "name: <object>" --include='*.yaml' .`), give that file's repo-relative path as `remediation.path`, and rewrite it as the object's complete desired manifest. Never emit a patch fragment. A file carrying `metadata.name` and a partial `spec` is not valid `kubectl apply` input, and a second file claiming an object the repo already declares is a duplicate resource id that both Config Sync and Argo reject.
- **A remediation that _creates_ an object the cluster does not have — a new PDB (3.3), a new HPA — goes beside the declaration of the workload it protects.** That workload is what the new object selects, so it is always the anchor and it always exists: narrow by namespace first (`grep -rl "namespace: <namespace>" --include='*.yaml' .`), then **open the hits and confirm one actually declares that workload on the target cluster** before writing beside it, matching the naming style of the files already there. Do not anchor on `grep "name: <workload>"` alone — `grep` is kind-blind and unanchored, so it also matches `app.kubernetes.io/name:` label lines and any object whose name merely starts with the workload's, and it will happily return a file under another cluster's directory. **If the hits land in more than one directory, or none can be tied to the target cluster, the finding is `kind: manual`** — writing a namespaced object into the wrong cluster's applied tree fails that tree's sync, which is worse than writing nothing. **Never create a new top-level directory, and never write to a path whose parent directory does not already exist in the clone.** The repository is reconciled by a tool that applies a fixed set of paths, and it is not this SOP's business to know which — that is why the destination is discovered from the workload rather than named here. A file outside those paths is applied by nothing: it merges clean, closes the finding for one run, and leaves the workload exactly as exposed as it was.
- **If the workload itself has no declaration in the clone, the finding is `kind: manual`.** There is no sibling to anchor to, and a workload this repository does not declare cannot be given a PDB by a pull request against it. Put the manifest you would have written in `recommendation.action`.
- **If the object already exists and you cannot find its declaration, the finding is `kind: manual`.** Put the intended change in `recommendation.action`, write no file, omit `remediation.path`. Never invent a new path for an object that may already be declared somewhere you did not look.
- Either way the path is POSIX and relative to the `workspace` clone from Step 0: no leading `/` or `:`, no `..`, and no glob metacharacters (`*`, `?`, `[`, `]`). The helper rejects all of those outright. A path with no file behind it is handled more gently but is still your mistake: that finding degrades to `kind: manual`, keeps its evidence and recommendation, and the ledger records that the audit named the fix without writing it. The report publishes; the fix just cannot travel in a PR. Name a new file for the object it creates and the workload it protects (`<workload>-pdb.yaml`), not for the finding id: it lands in a directory of hand-named manifests where a finding id reads as noise, and finding ids are re-derived every run while the file has to stay put across them.
- Write the manifest for every id in `pending_remediation_requests` from Step 0 whose finding still reproduces. A human has already asked for that fix; without the file the promotion fails.
- Head each file with a comment naming the cluster, the check, and the finding id.
- Copy selectors and labels verbatim from the live object. **Never invent a resource quantity, replica count, utilisation target, or probe endpoint** — if the value cannot be read off the object or is not structurally safe (`maxUnavailable: 1`, `maxSkew: 1`), the finding is `kind: manual`.
- Writing the file does not open a Pull Request. `finish` opens one automatically only for a `critical` finding whose remediation is a `manifest` and whose branch carries no **live** pull request, at most five per run; the ledger names the ones it withheld. "Live" is the operative word: a PR the harness closed itself as stale is labelled `audit:stale-closed` and the same fix may be promoted again, while a PR a human closed and a PR that merged both stay closed — the harness does not overrule either. Everything else waits for a repo writer to comment `/remediate <finding-id>` or `/remediate all` on the ledger issue. In this audit only 3.4 produces a `critical` manifest, so a drain-blocking PDB that the repo already declares is the one fix that normally arrives ready to merge; one that is not declared anywhere stays `manual`. A `kind: manual` finding is never promotable — it stays prose in the ledger. Findings whose manifest paths intersect share one PR.
- These files are proposals for human review; do not `kubectl apply` anything, ever.

### 5. Emit findings.json

Write the schema exactly as the helper validates it to the `findings_path` returned in Step 0: `audit` set to `obtainability-audit`; `scope.clusters` non-empty, each entry carrying the mandatory `checks_run` list of `{check, command}` objects for the §3 checks that actually ran there and optionally a non-empty `limitations` string; `scope.skipped` complete and disjoint from `scope.clusters`; and, for each finding, `check` (the §3 slug that produced it), `severity`, `title`, `cluster`, `namespace`, `object` (as `Kind/name`), `evidence.command` (the literal confirm command you ran) and `evidence.excerpt` (trimmed to the few lines that prove the finding), `impact`, `recommendation`, and `remediation` — with `remediation.path` present and the file on disk whenever `kind == "manifest"`. No `id`: the harness derives it (§3) from `check`, `cluster`, `namespace` and `object`. Sort findings by severity (`critical`, `major`, `minor`), then cluster, then namespace, so the run-over-run diff stays readable. A schema violation publishes nothing: `finish` exits 2 and the ledger is untouched. Validate your own JSON before calling it.

Read your `checks_run` lists once more before you write. Padding one to eleven because §3 lists eleven checks is the one entry in this document that converts a partial audit back into a false all-clear — the harness cannot see the check you skipped, so it takes the list at its word. The `command` on each entry is what makes that padding falsifiable rather than free: it is published verbatim, so an invented command is a false statement in a public issue. `checks_not_applicable` is the same lie wearing a different field: it removes checks from the denominator, so a slug parked there because you ran out of turns is a coverage gap the ledger will never show. It is published too — every exclusion and its reason render under _Not applicable_, where a reviewer who knows the cluster can call it. An honest nine-of-eleven costs you nothing but an open ledger, and an honest nine-of-nine on a cluster where the other two cannot apply closes it.

**`recommendation` is required on every finding.** Three sub-fields, all non-empty strings, no exceptions — a `manual` finding that will never become a PR needs it exactly as much as a promotable one. The reasoning is only cheap while the evidence is still in front of you; deferring it to promotion time means writing it blind.

- `action` — what to do, imperative, one or two sentences.
- `rationale` — why this fix and not the obvious alternative; name the alternative you considered and why you rejected it.
- `risk` — what breaks on apply, and the read-only check to run first.

Worked example, for a 3.3 finding on `payments/api`:

```json
"recommendation": {
  "action": "Add a PodDisruptionBudget with maxUnavailable: 1 for the payments/api Deployment, its spec.selector copied verbatim from the Deployment's own selector.",
  "rationale": "maxUnavailable: 1 is structurally safe at any replica count >= 2. The obvious alternative, minAvailable, is rejected: an integer minAvailable at or above the replica count is exactly the drain deadlock check 3.4 reports, and a budget that is safe today becomes one the next time the owner scales down.",
  "risk": "Once a PDB exists the eviction API refuses the last permitted replica, so draining an already-degraded workload waits instead of proceeding. Confirm the Deployment is at its full replica count first with kubectl --context prod-us-east get deploy -n payments api -o wide."
}
```

### 6. Close the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py finish --audit obtainability-audit \
  --findings-file /opt/data/scratch/findings_obtainability-audit.json
```

One JSON line comes back, carrying `status`, `issue_url`, `new`, `resolved`, `prs_opened`, `prs_closed`, `partial`, `coverage_gaps`, and `silent_ok`. Exit 2 means the validator rejected the document and nothing was published — fix the document, do not retry blind. Exit 1 is fatal. Exit 0 means it published.

`partial` is `true` when the run could not read the whole fleet: any cluster in `scope.skipped`, or any cluster kept in scope with a `limitations` note. `coverage_gaps` names each one in a sentence. The harness then refuses to draw conclusions from silence, because a workload you never queried is not a workload that got its PDB: `resolved` comes back `0` and no resolved-delta is posted, no remediation PR is retired as stale, and the ledger issue stays open even at zero findings — `status` is still `CLEAN`, but the issue survives with a comment naming what went unread. A check declared in `checks_not_applicable` is not a gap and does not raise the flag; it left the denominator. Nothing else raises it — it is `true` if and only if `coverage_gaps` is non-empty. A fleet big enough that the description had to drop findings is not a coverage gap: those workloads were queried, the title counts them, and the body says which ones it left out.

**`silent_ok` decides silence. Do not re-derive it.** `finish` returns `silent_ok: true` only when this run moved nothing an operator needs to hear about: nothing new, nothing resolved, no coverage gap, no remediation PR opened or closed. Read the flag rather than reassembling that from `status`, `new`, `resolved`, and `partial` yourself — that arithmetic is where a run talks itself into silence it has not earned. Two rules, and they are the whole rule:

- On a **scheduled** run, `silent_ok: true` → your entire final response is exactly `[SILENT]`. Otherwise report, and every report carries `issue_url` in full.
- **An on-demand run is never silent.** If a person dispatched this job — from a kanban card or straight from chat — someone is waiting on the answer, and `[SILENT]` throws it away. Report the outcome and the ledger URL whatever `silent_ok` says.

What to report in each case:

- `silent_ok: true` — `[SILENT]` on a scheduled run, nothing else and no preamble. On `CLEAN` the ledger issue closed as completed and every open remediation PR for this stream closed with it; on `UPDATED` the ledger was rewritten but nothing moved. Dispatched on demand, say which in one line and give the issue URL.
- `status: "CLEAN"` with `resolved: > 0` — every reliability gap this ledger tracked has been closed. Report the issue URL and the count. A fleet that just became drain-safe is news, and it is the only good news this audit has.
- `status: "CLEAN"` with `partial: true` — nothing reproduced, but the ledger and its PRs stayed open because the coverage was incomplete. One line, the clean result plus the `coverage_gaps`, then stop.
- Any other outcome — reply with **one line**: counts by severity, new vs. resolved, skipped-cluster count if any, remediation PRs opened or closed, and the `issue_url`. Example: `Workload Reliability Audit: 2 critical, 6 major, 11 minor across 4 clusters (3 new, 1 resolved, 1 skipped, 1 remediation PR opened) — <issue_url>`. A partial audit that reads as a complete one is worse than no audit.

## Red Lines

- **Read-only against every cluster.** No `apply`, `patch`, `edit`, `delete`, `scale`, `drain`, `cordon`, or eviction — including dry-runs that reach the eviction API.
- **No hand-written issue or PR bodies, and no direct git/gh calls.** `audit_report.py` owns the ledger issue, the remediation branches, the commits, and every body it renders. Never open a second ledger issue for this stream, never open a remediation PR for a non-`manifest` finding, and never reopen a merged one.
- **No credentials in evidence.** A Secret's `data:` block, a token, or a private key never enters an excerpt; re-read with a projection that omits it.
- **A finding you cannot reproduce is dropped, not softened.** `evidence.command` is the literal command you executed; if the confirm read fails or the condition has cleared, the finding does not ship.
- **No fabricated numbers.** Resource quantities, replica counts, autoscaling targets, and probe endpoints are either read off the live object or left to a human.
- **No forbidden sources.** BigQuery, Prometheus/GMP, VPA recommendations, Policy Controller, and external blueprints are out of scope; so is delegating any part of this audit to a Cluster Agent.
- **Stable ids or the delta lies.** An unstable id — one that varies between runs because the `object` it is derived from moved — turns one persistent problem into an infinite stream of "new" findings.
