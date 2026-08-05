# SOP: Security & RBAC Posture Audit (Daily Governance)

**Purpose:** A read-only, fleet-wide security sweep of every managed GKE cluster. Detects privilege-escalation surfaces, over-broad RBAC, missing network isolation, and cluster-level identity misconfiguration, then emits reproducible findings into one GitHub issue — the stream's ledger — with narrow remediation Pull Requests hung off it. Cron id `compliance-audit`, schedule `20 6 * * *` (daily 06:20 UTC).

**Data sources:** `kubectl` read verbs, `gcloud container clusters|node-pools list|describe`, and the `gke` MCP server. Nothing else. There are **no external inputs** — no blueprint, no CMDB, no BigQuery, no Prometheus, no Policy Controller / Gatekeeper, no Security Command Center, no kanban delegation to Cluster Agents. Every finding comes from a command this SOP runs itself, in this run.

---

## Execution Checklist

### 0. Open the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py start --audit compliance-audit
# -> {"issue": <int|null>, "repo":"org/repo", "workspace":"/opt/data/gitops/compliance-audit/org__repo",
#     "findings_path":"/opt/data/scratch/findings_compliance-audit.json",
#     "pending_remediation_requests":["<finding-id>", ...]}
```

`findings_path` is the only file you write findings to. `issue` is the stream's open ledger issue, or `null` when the stream has none. `pending_remediation_requests` is the set of finding ids a repo writer asked for with a `/remediate` comment on the ledger — write a `kind: manifest` file for every one of them during §2 and §3, whether or not this SOP would have promoted it on its own.

`workspace` is the GitOps clone. You do **not** start in a checkout: `start` clones the repository to that directory itself, and every `remediation.path` in §3 is resolved against it — a manifest written anywhere else is a file the harness never sees. There is no report branch, and `start` creates none. Do not run `git`, `gh`, or `submit-suggestion` anywhere in this SOP — `audit_report.py` owns the write path and renders the ledger issue and every remediation PR. **Never hand-write an issue body or a PR body.**

### 1. Enumerate the target fleet

```bash
PROJECT=$(gcloud config get-value project)
gcloud container clusters list --project="$PROJECT" \
  --format='json(name,location,status,autopilot.enabled,currentMasterVersion)'
```

For each cluster with `status == RUNNING`, pin a per-cluster kubeconfig (local-only, mutates nothing) the way `platform_mcp_server.switch_kube_context` does, then confirm read access:

```bash
export KUBECONFIG="$HERMES_HOME/.kubeconfigs/kubeconfig_${PROJECT}_${C}_${L}.yaml"
gcloud container clusters get-credentials "$C" --location="$L" --project="$PROJECT"
kubectl auth can-i list pods --all-namespaces
```

Every cluster you actually query goes in `scope.clusters` as `{name, location, project, checks_run}`. `scope.clusters` must be non-empty — if enumeration returns nothing or every cluster fails, do **not** emit an empty-scope file; stop and report the enumeration failure.

**`checks_run` is mandatory on every cluster, and it is the record of what you actually did.** Each entry is an object, never a bare string:

```json
{
  "check": "netpol-missing",
  "command": "kubectl --context prod-us-east get networkpolicy -A -o custom-columns=NS:.metadata.namespace --no-headers"
}
```

`check` is the backticked slug from the §2 heading that defines it — `privileged-container`, `netpol-missing`, and so on — never the section number and never prose. (`start` prints the full roster of eleven, so you never have to reconstruct it from memory; the SOP still says what each check _is_.) `command` is the literal invocation you issued on that cluster for that check, carrying its `--context`/`--project` and the namespace or resource it targeted. It must name one of `kubectl`, `gcloud`, `gsutil`, `bq`, `helm`, or `curl`; `echo`, `cat`, `python3 -c`, and a call back into `audit_report.py` are all rejected, as is anything under eight characters.

The validator rejects an unknown slug, a duplicate, a missing or unusable command, the field being absent, and an empty list unless that cluster's `limitations` says why nothing ran: a cluster you could reach but ran nothing against is not a clean cluster, it is an audit that did not happen, and it must never publish as an all-clear. Anything short of the checks that apply to that cluster makes the run **partial**, exactly as a `limitations` note does — the ledger stays open, nothing is announced as resolved, and no remediation PR is closed. Append the entry when the check completes, not when you plan to run it, and paste the command rather than reconstructing it afterwards: every command lands verbatim in the ledger under _How this run checked the fleet_, where a reviewer can re-run it.

**A check the cluster's shape rules out is not a gap — declare it.** Alongside `checks_run`, a cluster may carry `checks_not_applicable` as a list of `{check, reason}`:

```json
{
  "check": "legacy-metadata",
  "reason": "GKE Autopilot: no user-managed node pools to carry a metadata setting."
}
```

Same slugs as `checks_run`, and the `reason` has to say why the check _cannot_ apply here — "N/A" and "not applicable" are rejected; name the property of the cluster that rules it out. Those checks leave the denominator rather than counting as missing, so an Autopilot cluster where 2.1–2.3 and 2.9 cannot apply reads as complete at seven of seven instead of forever-incomplete at seven of eleven. Without this the ledger can never close on an Autopilot fleet, nothing is ever announced as resolved, and no stale remediation PR is ever cleaned up. Use it only for checks the cluster's shape rules out. A check you could have run and did not is a `limitations` note and a real gap, and the validator rejects a slug that appears in both lists, a duplicate, an unknown slug, and a reason under sixteen characters.

**The one-question scope rule.** A cluster appears in exactly one scope list. Could you read it? Yes → `scope.clusters`; if some checks did not run there, split them — the ones that could have run and did not go in that cluster's `limitations`, the ones its shape rules out in `checks_not_applicable`. No → `scope.skipped`. Nothing goes in both, and nothing in `scope.skipped` may appear in a finding.

`scope.skipped` is only for clusters you could **not** read, as `{cluster, reason}`:

- `status != RUNNING` → `"cluster status <STATUS>, not queried"`.
- `get-credentials` / `auth can-i` fails → `"no read access: <trimmed stderr>"`. Never infer a finding from a cluster you could not reach.

`scope.clusters[].limitations` is an optional string on a cluster you **did** read, naming the checks that _could_ have run there and did not, and why. When present it must be non-empty and must name the checks by number. It is the coverage flag, so keep it for what a later run could clear: a check the cluster's shape rules out belongs in `checks_not_applicable` instead. Partial coverage is never a reason to move the cluster to `scope.skipped` — that would suppress every real finding from the checks that _did_ run.

- **Autopilot** (`autopilot.enabled == true`): checks 2.1–2.3 are rejected by admission and 2.9 has no user-managed node pools, so all four are inapplicable. The cluster still belongs in `scope.clusters`, and those four go in its `checks_not_applicable`, one entry each:

  ```json
  [
    {
      "check": "privileged-container",
      "reason": "GKE Autopilot: privileged containers are rejected at admission and cannot exist here."
    },
    {
      "check": "host-namespace",
      "reason": "GKE Autopilot: hostPID/hostIPC/hostNetwork are rejected at admission and cannot exist here."
    },
    {
      "check": "hostpath-mount",
      "reason": "GKE Autopilot: hostPath volumes are rejected at admission and cannot exist here."
    },
    {
      "check": "legacy-metadata",
      "reason": "GKE Autopilot: no user-managed node pools to carry a metadata setting."
    }
  ]
  ```

  **Do not also name those four in `limitations`.** That field is the coverage flag: any non-empty string in it makes the run `partial`, and a fact about the cluster's shape does not stop being true next week, so the daily security stream would be pinned at `partial: true` forever — `resolved: 0`, no stale remediation PR ever closed, the ledger never able to retire. `checks_not_applicable` says the same thing without that cost, because it takes the four out of the denominator instead of adding them to the gaps. Checks 2.4–2.8, 2.10 and 2.11 run there exactly as on a Standard cluster and **their findings are real** — an Autopilot cluster is audited, not skipped. A privileged-container finding on Autopilot is a false positive by construction; a missing-NetworkPolicy finding on Autopilot is not.

- Any other gap on a reachable cluster — a check whose command errored, an API group that is absent — is the other case, and that one _is_ a `limitations` note: name the check and the reason there. The test is whether re-running the audit could close it. A command that errored could succeed tomorrow, so it is a gap; Autopilot's admission policy could not, so it is not.

### 2. Checks

Shared setup, evaluated once per cluster. `$PRE` normalises every auditable workload to `{kind, ns, name, spec}` and applies the universal suppressions, so each workload check below is `$WL | jq -r --arg sys "$SYS" "$PRE"'| <filter>'`.

```bash
SYS='^(kube-system|kube-public|kube-node-lease|gke-.*|gmp-system|gke-gmp-system|gke-managed-.*|cnrm-system|configconnector-operator-system|krmapihosting-system|istio-system|asm-system|anthos-identity-service|config-management-.*|gatekeeper-system|composer-system)$'
WL='kubectl get deploy,sts,ds,cronjob,pod -A -o json'
PRE='.items[]
 | select((.metadata.namespace|test($sys)|not)
      and (.kind!="Pod" or ((.metadata.ownerReferences//[])|length)==0)
      and (((.metadata.labels//{})["addonmanager.kubernetes.io/mode"] // (.metadata.annotations//{})["components.gke.io/component-name"])==null))
 | {kind, ns:.metadata.namespace, name:.metadata.name,
    spec:(.spec.template.spec // .spec.jobTemplate.spec.template.spec // .spec)}'
```

**Universal suppressions — every check in this section:** namespaces matching `$SYS`; objects carrying `addonmanager.kubernetes.io/mode` or `components.gke.io/component-name` (the GKE-managed add-ons — `fluentbit-gke`, `gke-metrics-agent`, `pdcsi-node`, `netd`, `anetd`, `ip-masq-agent`, `konnectivity-agent`, `gke-metadata-server`, `nvidia-gpu-device-plugin`; flagging these is the fastest way to get this audit switched off); pods with a non-empty `ownerReferences` — audit the **owning controller**, never the pod, because pod name suffixes are random. `kubeagents-system` is deliberately **not** suppressed: the harness audits itself.

**Finding identity.** **Do not write an `id`.** The harness derives it from `check`, `cluster`, `namespace` and `object`, and ignores any `id` in the file. Set those four correctly and identity takes care of itself; get one of them wrong and you have renamed the finding, which the ledger reads as the old one being fixed.

`check` is the backticked token in the `####` heading of the check that produced the finding — the slug, not a section number or a description. A slug outside this SOP's roster is rejected.

One finding per (check, object): three privileged containers in one Deployment are **one** finding listing all three in `evidence.excerpt`. Two findings that agree on all four identity fields are the same finding, and the harness refuses the document rather than silently collapsing them.

`object` is therefore load-bearing, and it must name the thing the check actually judged. 2.5 judges the **Role or ClusterRole** that carries the wildcard, so `object` is that role — not the binding that grants it, which merely proves the role is live. Naming the binding one run and the role the next is the same problem wearing two names, and the ledger reports the switch as a fix.

**Evidence.** `evidence.command` is mandatory and must be the literal command run, with `$WL`/`$SYS`/`$PRE` expanded so a human can paste it unchanged. **A finding you cannot reproduce is dropped, not softened** — there is no "possible" severity.

**Credential hygiene.** Never paste a Secret's `data:` block, a ServiceAccount token, a kubeconfig, or a private key into `evidence.excerpt`. Re-run the command with a field selector or an `-o jsonpath` that omits the value and quote that output instead — the object reference proves the finding, the credential never does. The harness redacts high-confidence credential shapes as a backstop, not as the primary control.

#### 2.1 Privileged containers (`privileged-container`)

```bash
$WL | jq -r --arg sys "$SYS" "$PRE"'
 | [((.spec.containers//[])+(.spec.initContainers//[]))[]
     | select(.securityContext.privileged==true or ((.securityContext.capabilities.add//[])|index("SYS_ADMIN"))!=null)
     | .name] as $bad
 | select(($bad|length)>0) | "\(.kind)/\(.ns)/\(.name): \($bad|join(","))"'
```

- **Flag when:** a container or initContainer sets `securityContext.privileged: true`, or adds capability `SYS_ADMIN`.
- **Do NOT flag:** universal suppressions; CSI node drivers and CNI agents shipped as GKE add-ons; Autopilot clusters — the check does not run there and §1 records that in the cluster's `checks_not_applicable`, not its `limitations`; `allowPrivilegeEscalation: true` on its own — that is the Kubernetes default and would fire on nearly every workload.
- **Severity:** `critical` — a privileged container is one escape away from owning the node and every workload on it.
- **Impact:** "Container has full host device and kernel access; compromising this workload compromises the node."
- **Remediation:** `kind: manual`. Dropping privilege can break a workload that needs one specific capability, so the owner confirms. Note the minimal replacement: remove `privileged`, add only the required `capabilities.add` entries.

#### 2.2 Host namespace sharing (`host-namespace`)

```bash
$WL | jq -r --arg sys "$SYS" "$PRE"'
 | select(.spec.hostNetwork==true or .spec.hostPID==true or .spec.hostIPC==true)
 | "\(.kind)/\(.ns)/\(.name): hostNetwork=\(.spec.hostNetwork//false) hostPID=\(.spec.hostPID//false) hostIPC=\(.spec.hostIPC//false)"'
```

- **Flag when:** the pod spec sets `hostNetwork`, `hostPID`, or `hostIPC` to `true`.
- **Do NOT flag:** universal suppressions; Autopilot clusters (§1 `checks_not_applicable`); ingress/gateway data-plane DaemonSets that legitimately bind host ports — verify `hostNetwork` is the only flag set **and** a `hostPort` is declared, then record `minor` rather than suppressing silently.
- **Severity:** `critical` when `hostPID` or `hostIPC` is set (direct visibility into other tenants' processes and memory); `major` when only `hostNetwork` is set — it bypasses NetworkPolicy enforcement and exposes node loopback, but does not cross the process boundary.
- **Impact:** "Workload shares the node's process/IPC/network namespace, bypassing pod isolation and NetworkPolicy enforcement."
- **Remediation:** `kind: manual`. Name the field to remove; for `hostNetwork`, note that a `NodePort` Service or a Gateway listener is the supported replacement for `hostPort`.

#### 2.3 hostPath volume mounts (`hostpath-mount`)

```bash
$WL | jq -r --arg sys "$SYS" "$PRE"'
 | [(.spec.volumes//[])[]|select(.hostPath)|{n:.name,p:.hostPath.path}] as $hv | select(($hv|length)>0)
 | [((.spec.containers//[])+(.spec.initContainers//[]))[]|(.volumeMounts//[])[]|{n:.name,ro:(.readOnly//false)}] as $m
 | [$hv[] as $v | ($m[]|select(.n==$v.n)|"\($v.p) readOnly=\(.ro)")] as $used | select(($used|length)>0)
 | "\(.kind)/\(.ns)/\(.name): \($used|join("; "))"'
```

- **Flag when:** the pod spec declares a `hostPath` volume that a container actually mounts.
- **Do NOT flag:** universal suppressions; Autopilot clusters (§1 `checks_not_applicable`); a declared-but-unmounted `hostPath`; the log-shipper pattern (`/var/log`, `/var/lib/docker/containers`) when **every** mount of it is `readOnly: true` — record those `minor`.
- **Severity:** `critical` when the path is `/`, `/etc`, `/proc`, `/var/run/docker.sock`, `/run/containerd/containerd.sock`, or under `/var/lib/kubelet`, **or** when any mount of it is writable — those are node takeover or credential theft. `major` otherwise: still a persistence and cross-tenant leak path.
- **Impact:** "Workload mounts a node filesystem path, giving it access to state belonging to the node and to other tenants' pods."
- **Remediation:** `kind: manual`. Name the replacement — a PersistentVolumeClaim, a ConfigMap/Secret projection, or the CSI driver appropriate to the data.

#### 2.4 `cluster-admin` bound to non-system subjects (`cluster-admin-binding`)

```bash
kubectl get clusterrolebindings -o json | jq -r '.items[]
 | select(.roleRef.name=="cluster-admin") | . as $b | (.subjects//[])[]
 | select((.kind=="ServiceAccount" and ((.namespace//"")|test("^(kube-system|gke-.*|gmp-system|cnrm-system|configconnector-operator-system|krmapihosting-system|config-management-.*)$")|not))
       or ((.kind=="User" or .kind=="Group") and ((.name|startswith("system:"))|not)
           and ((.name|test("^(gke-|service-[0-9]+@)|gserviceaccount\\.com$"))|not)))
 | "\($b.metadata.name) -> \(.kind)/\(.namespace//"-")/\(.name)"'
```

- **Flag when:** a ClusterRoleBinding to `cluster-admin` names a ServiceAccount outside the system namespaces above, or a `User`/`Group` that is neither a `system:` principal nor a Google-managed service identity.
- **Do NOT flag:** `Group/system:masters` (the GKE bootstrap binding); GKE-installed `gce:*` / `system:*` bindings; `cnrm-system/cnrm-controller-manager`, which requires it by design. A `Group` that is an organisation email domain is an intentional human-admin grant — downgrade to `minor` and name the group rather than suppressing it.
- **Severity:** `critical` — a `cluster-admin` ServiceAccount turns any pod compromise in its namespace into full cluster compromise.
- **Impact:** "Subject holds unrestricted read/write on every resource in the cluster, including Secrets in every namespace."
- **Remediation:** `kind: manual`. Give the binding name, the subject, and the verification step a human runs first: `kubectl auth can-i --list --as=system:serviceaccount:<ns>:<sa>`, then delete the binding and substitute a scoped Role.

#### 2.5 Wildcard verbs/resources in bound Roles and ClusterRoles (`wildcard-rbac`)

```bash
kubectl get clusterroles,roles -A -o json | jq -r '.items[]
 | select(((.metadata.labels//{})["kubernetes.io/bootstrapping"])!="rbac-defaults" and ((.metadata.name|startswith("system:"))|not))
 | . as $r | [(.rules//[])[]|select(((.verbs//[])|index("*"))!=null
     and (((.resources//[])|index("*"))!=null or ((.apiGroups//[])|index("*"))!=null))] as $w
 | select(($w|length)>0) | "\($r.kind)/\($r.metadata.namespace//"-")/\($r.metadata.name): \($w|tojson)"'
kubectl get clusterrolebindings,rolebindings -A -o json | jq -r '.items[]
 | "\(.roleRef.kind)/\(.roleRef.name) <- \(.kind)/\(.metadata.name) subjects=\([(.subjects//[])[]|"\(.kind):\(.namespace//"-"):\(.name)"]|join(","))"'
```

Intersect the two and report only wildcard roles the second command shows bound to a non-system subject (same subject test as 2.4). An unbound over-broad role grants nothing.

- **Flag when:** a Role/ClusterRole has a rule with `verbs: ["*"]` **and** `resources: ["*"]` or `apiGroups: ["*"]`, and a binding grants it to a non-system subject.
- **Do NOT flag:** roles labelled `kubernetes.io/bootstrapping=rbac-defaults` or named `system:*`; **unbound** roles; a wildcard confined to one vendor apiGroup (`apiGroups: ["kubeagents.io"], resources: ["*"]`) — that is the operator-owns-its-own-CRDs pattern, not an escalation. A wildcard over the core group (`apiGroups: [""]`) is never suppressed.
- **Severity:** `critical` in a ClusterRole (fleet-wide blast radius); `major` in a namespaced Role, where damage is bounded to one tenant.
- **Impact:** "Subject can perform any verb on any resource in this scope, including reading Secrets and creating privileged pods — an unbounded escalation path."
- **Remediation:** `kind: manual`. Include the `kubectl auth can-i --list --as=...` output as the starting point for an enumerated replacement rule set.

#### 2.6 Namespaces with no enforcing NetworkPolicy (`netpol-missing`)

```bash
comm -23 \
  <(kubectl get ns -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | grep -Ev "$SYS" | sort) \
  <(kubectl get netpol -A -o jsonpath='{range .items[*]}{.metadata.namespace}{"\n"}{end}' | sort -u)
kubectl get netpol -A -o json | jq -r '.items[]
 | select(.spec.podSelector=={} and (((.spec.ingress//[])|any(.=={})) or ((.spec.policyTypes//[])|length)==0))
 | "\(.metadata.namespace)/\(.metadata.name): allow-all"'
```

- **Flag when:** a non-system namespace has **zero** NetworkPolicies, or every policy in it is an allow-all (`podSelector: {}` with an empty ingress rule). Both are a default-allow posture.
- **Do NOT flag:** universal suppressions; namespaces with zero workloads (`kubectl get pods -n <ns> --no-headers | wc -l` is `0`) — no exposure, pure churn; namespaces already covered by a cluster-wide policy under Dataplane V2 (`kubectl get ccnp -o name`).
- **Severity:** `major` for zero policies — unrestricted lateral movement, though the namespace boundary and RBAC still hold. `minor` for allow-all-only: the team engaged with NetworkPolicy and the fix is a one-line edit.
- **Impact:** "Every pod in this namespace accepts traffic from every pod in the cluster; a compromise anywhere reaches these workloads unimpeded."
- **Remediation:** the two flag conditions are two different problems, and only one of them is fixed by adding a file.
  - **Zero policies** — the object does not exist, so §3's create rule applies: `kind: manifest`, with §3 deciding the path by finding where this namespace is already declared. Generate **exactly one** `NetworkPolicy`, `default-deny-ingress` (`podSelector: {}`, `policyTypes: [Ingress]`, no `ingress` rules), and nothing else.
  - **Allow-all only** — the offending policy _is_ the finding, and adding a second file does not fix it. NetworkPolicy is additive: a pod is reachable if **any** policy selecting it permits the traffic, so a deny-everything policy sitting alongside an allow-all one changes nothing. Emitting it produces a pull request that merges cleanly, closes the finding for exactly one run, and leaves the namespace as open as it was — worse than no fix, because it also spends the reviewer's trust. Name the allow-all policy in `object` as `NetworkPolicy/<name>` and fix _that_ object, under §3's change-an-existing-object rule: `kind: manifest` **only** when the GitOps repo already declares it — `remediation.path` is that existing file, rewritten as the policy's complete desired manifest with the empty `ingress` rule removed (`podSelector: {}`, `policyTypes: [Ingress]`, no `ingress`) and its name unchanged — and `kind: manual` otherwise, because the harness will not edit or delete a live object it cannot find declared. Never write a second file for this branch.

  In both branches `remediation.note` says the policy is ingress-only and that the team adds per-service allow rules before merge. **Never add a second policy that lists `Egress` in `policyTypes`** — including an "allow DNS" companion. Any policy naming `Egress` makes every pod it selects egress-isolated, so pairing one with the default-deny does not soften it: it default-denies all outbound traffic for those pods and permits only what that policy allows. Egress isolation is a separate, deliberate, breaking change, and this audit does not make it.

#### 2.7 Default ServiceAccount token automounting (`default-sa-automount`)

```bash
kubectl get sa -A --field-selector metadata.name=default -o json \
  | jq -r '.items[]|select(.automountServiceAccountToken!=false)|.metadata.namespace'
$WL | jq -r --arg sys "$SYS" "$PRE"'
 | select(((.spec.serviceAccountName // .spec.serviceAccount)//"default")=="default")
 | select(.spec.automountServiceAccountToken!=false) | "\(.kind)/\(.ns)/\(.name)"'
```

- **Flag when:** a workload resolves to the `default` ServiceAccount **and** neither the pod spec nor the `default` SA object sets `automountServiceAccountToken: false`. Both commands must agree — the SA-level setting suppresses the pod-level default.
- **Do NOT flag:** universal suppressions; workloads using a dedicated named ServiceAccount — a mounted token there is intentional, and whether its RBAC is right is 2.4/2.5's job; namespaces whose `default` SA already sets `automountServiceAccountToken: false`.
- **Severity:** `major` — the mounted token is a live API credential handed to a workload that by definition did not ask for one, and it is the standard first move after a container compromise.
- **Impact:** "Workload mounts an API-server credential it does not use, handing an attacker an authenticated foothold for free."
- **Remediation:** the namespace's `default` ServiceAccount with `automountServiceAccountToken: false`. That ServiceAccount already exists in the cluster, so §3's rule applies: `kind: manifest` only when the repo already declares it — rewrite that declaration complete — and `kind: manual` otherwise. One file fixes every workload in that namespace, so emit it once per namespace and point all of that namespace's findings at the same path.

#### 2.8 Workload Identity not enabled on the cluster (`workload-identity-off`)

```bash
gcloud container clusters describe "$C" --location="$L" --project="$PROJECT" \
  --format='json(workloadIdentityConfig.workloadPool,nodeConfig.serviceAccount)'
```

- **Flag when:** `workloadIdentityConfig.workloadPool` is absent or empty.
- **Do NOT flag:** Autopilot clusters — the check runs there, but Workload Identity is always on and the field always populated, so it simply never fires; clusters in `scope.skipped`, which you could not read and about which you therefore have no evidence either way. A `limitations` note is **not** a suppression: on a cluster in `scope.clusters`, run every check its `limitations` string does not name.
- **Severity:** `critical` — without it every pod authenticates to Google Cloud as the node service account, so all workloads on a node share one identity and pod-level IAM is impossible.
- **Impact:** "All pods on this cluster share the node service account's Google Cloud permissions; there is no per-workload IAM boundary."
- **Remediation:** `kind: gcloud` — `gcloud container clusters update <C> --location=<L> --project=<PROJECT> --workload-pool=<PROJECT>.svc.id.goog`. Note that node pools must then move to `GKE_METADATA` (2.9) and that this recreates nodes.
- **Ownership.** This check owns the Workload Identity verdict for the whole fleet; the Fleet Consistency Drift audit defers its `workload-identity` facet here (its §4.2) rather than reporting the same cluster in a second ledger. An absolute check is strictly stronger than a majority vote — a fleet that has Workload Identity off everywhere produces no drift finding at all, and still every cluster is wrong.

#### 2.9 Node pool exposes the legacy GCE metadata endpoint (`legacy-metadata`)

```bash
gcloud container node-pools list --cluster="$C" --location="$L" --project="$PROJECT" \
  --format='value(name,config.workloadMetadataConfig.mode)'
```

- **Flag when:** a node pool's `config.workloadMetadataConfig.mode` is empty or `GCE_METADATA` — metadata concealment is off and any pod can read `169.254.169.254`.
- **Do NOT flag:** Autopilot clusters — there are no user-managed node pools, and §1 records that in the cluster's `checks_not_applicable`; pools already reporting `GKE_METADATA`. Detection is configuration-only **by design** — probing the endpoint live would need `kubectl run`/`exec`, both write verbs forbidden by the Red Lines, and the node pool mode is authoritative for this control anyway.
- **Severity:** `critical` — one unauthenticated HTTP request from any pod to a node-wide credential.
- **Impact:** "Any pod on this node pool can read the node service account's access token from the legacy metadata endpoint and escalate to that identity's full Google Cloud permissions."
- **Remediation:** `kind: gcloud` — `gcloud container node-pools update <POOL> --cluster=<C> --location=<L> --project=<PROJECT> --workload-metadata=GKE_METADATA`. Note that this drains and recreates the pool's nodes.

#### 2.10 Public control plane with no authorized networks (`public-control-plane`)

```bash
gcloud container clusters describe "$C" --location="$L" --project="$PROJECT" \
  --format='json(privateClusterConfig.enablePrivateEndpoint,masterAuthorizedNetworksConfig.enabled,masterAuthorizedNetworksConfig.cidrBlocks,controlPlaneEndpointsConfig.ipEndpointsConfig.enablePublicEndpoint)'
```

- **Flag when:** the public endpoint is reachable (`privateClusterConfig.enablePrivateEndpoint` not `true`, or `controlPlaneEndpointsConfig.ipEndpointsConfig.enablePublicEndpoint` is `true`) **and** either `masterAuthorizedNetworksConfig.enabled` is not `true` or its `cidrBlocks` contain `0.0.0.0/0`.
- **Do NOT flag:** clusters with `enablePrivateEndpoint: true` — there is no public endpoint, so authorized networks are moot; a narrow but unfamiliar CIDR list. Judging whether a specific CIDR _should_ be allowed needs an external source of truth this audit does not have; only a literally unrestricted list is a finding.
- **Severity:** `critical` — the API server is exposed to the entire internet with only credentials in front of it.
- **Impact:** "The cluster's API server accepts connections from any address on the internet; credential compromise or an API-server CVE is directly exploitable from outside the network."
- **Remediation:** `kind: gcloud` — `gcloud container clusters update <C> --location=<L> --project=<PROJECT> --enable-master-authorized-networks --master-authorized-networks=<CIDR[,CIDR...]>`. The CIDR list must come from a human; say so in `remediation.note` and do not invent one.

#### 2.11 Pod Security `restricted` profile gaps (`podsecurity-gaps`)

```bash
$WL | jq -r --arg sys "$SYS" "$PRE"'
 | . as $o | [((.spec.containers//[])+(.spec.initContainers//[]))[]
     | . as $c
     | (if (($c.securityContext//{})|has("runAsNonRoot")) then $c.securityContext.runAsNonRoot
        elif (($o.spec.securityContext//{})|has("runAsNonRoot")) then $o.spec.securityContext.runAsNonRoot
        else null end) as $nonroot
     | select(($nonroot!=true)
           or (($c.securityContext.runAsUser // $o.spec.securityContext.runAsUser)==0)
           or (((($c.securityContext.seccompProfile.type // $o.spec.securityContext.seccompProfile.type)//"")|test("^(RuntimeDefault|Localhost)$"))|not))
     | .name] as $bad
 | select(($bad|length)>0) | "\(.kind)/\(.ns)/\(.name): \($bad|join(","))"'
```

**Resolve `runAsNonRoot` with `has()`, never with `//`.** `//` is jq's _alternative_ operator: it fires on `false` exactly as it fires on `null`, so `(.securityContext.runAsNonRoot // $o.spec.securityContext.runAsNonRoot)` turns a container that explicitly sets `runAsNonRoot: false` over a compliant pod-level `true` into `true` — the check silently passes the one input it exists to catch. The `has()` ladder above distinguishes absent from false. `runAsUser` and `seccompProfile.type` keep `//` safely: neither `0` nor a string is falsy in jq, so the alternative fires only on a genuinely absent field.

- **Flag when:** a container neither inherits nor sets `runAsNonRoot: true` — **including a container that explicitly sets `runAsNonRoot: false` over a compliant pod-level default** — or explicitly sets `runAsUser: 0`, or has no `seccompProfile.type` of `RuntimeDefault`/`Localhost`.
- **Do NOT flag:** universal suppressions; any workload already reported by 2.1 — the privileged finding subsumes this one, never emit both; namespaces labelled `pod-security.kubernetes.io/enforce=restricted`, where admission already guarantees it.
- **Severity:** `minor` — these are defence-in-depth defaults rather than live escalation paths, and the fix is mechanical. Rating them `major` would drown the critical findings, which is how an audit becomes noise.
- **Impact:** "Containers run as root and/or without a seccomp filter, so a runtime escape has an unfiltered syscall surface and immediate root in the namespace it reaches."
- **Remediation:** the workload already exists, so §3's rule applies. When the repo declares it, `kind: manifest` at that declaration's own path, rewritten as the workload's **complete** desired manifest with `spec.template.spec.securityContext` set to `{runAsNonRoot: true, runAsUser: 10001, seccompProfile: {type: RuntimeDefault}}` and each container's `securityContext` to `{allowPrivilegeEscalation: false, capabilities: {drop: [ALL]}}`, everything else carried over unchanged. `remediation.note` states the UID is a placeholder the image owner must confirm. When you cannot find a declaration, `kind: manual` with the same change spelled out in `recommendation.action`.

### 3. Generate remediation artifacts

- Write every `kind: manifest` file into the `workspace` clone §0 named, **before** calling `finish`. A path with no file behind it no longer kills the run: that one finding degrades to `kind: manual`, keeps its evidence and recommendation, and says in the ledger that the audit named the fix but never wrote it — the report still publishes. Treat a degrade as a defect in your own work, not a fallback: it converts a fix a reviewer could have merged into one a human now writes by hand. This includes every finding named in `pending_remediation_requests` from §0 — a `/remediate` request with no manifest on disk cannot be promoted.
- **Where the file goes depends on whether the object already exists. Both branches discover a directory that is already there; neither invents one.**
- **Changing an object that already exists** — 2.7's `default` ServiceAccount, 2.11's workload, and 2.6's other branch where the namespace already has an allow-all policy — goes to that object's **existing declaration in the GitOps repo**: locate it (`grep -rl "name: <object>" --include='*.yaml' .`), name that file as `remediation.path`, and rewrite it as the object's complete desired manifest. Never write a patch fragment: a file carrying `metadata.name` and a partial `spec` is not valid `kubectl apply` input, and a second file claiming an object the repo already declares is a duplicate resource id that both Config Sync and Argo reject.
- **Creating an object the cluster does not have** — 2.6's NetworkPolicy on a namespace with **zero** of them — goes in the directory that already holds the applied declarations for that same cluster and namespace. **Find that directory, never assume it.** Anchor on the namespace, not on a name: `grep -rl "namespace: <namespace>" --include='*.yaml' .` finds the files declaring objects that live in it. Then **open what it returns and confirm it before you trust it** — the file must declare an object you actually observed on the target cluster in that namespace. Write the new file into that file's directory, named for the object it creates, matching the naming style of the files already there.
- **Never anchor with `grep "name: <namespace>"`.** `grep` is kind-blind and unanchored: `name: <ns>` also matches every `app.kubernetes.io/name:` label line and every object whose name merely starts with the namespace's, so it finds files that declare nothing in the namespace at all — including files under another cluster's directory, and control-plane files that no workload reconciler applies. A hit is not a declaration until you have opened it.
- **If the anchors land in more than one directory, or you cannot tie one to the target cluster, the finding is `kind: manual`.** Namespace names repeat across a fleet and `grep` does not know which cluster a directory feeds. Guessing puts a namespaced object into another cluster's applied tree, where the namespace may not exist — that fails the whole tree's sync, so it is worse than writing nothing.
- **Never create a new top-level directory, and never write to a path whose parent directory does not already exist in the clone.** This repository is reconciled by a tool that applies a fixed set of paths, and it is not this SOP's business to know which — that is exactly why the destination is discovered from a sibling rather than named here. A file outside those paths is applied by nothing: it merges clean, closes the finding for exactly one run, and leaves the namespace as open as it was. That is the same failure 2.6's allow-all branch describes, reached from the other direction, and it costs the same reviewer's trust.
- **A namespace nothing in the clone declares is not GitOps-managed, so the finding is `kind: manual`** — there is no sibling to anchor to, and a cluster this repository does not describe cannot be fixed by a pull request against it. Put the manifest you would have written in `recommendation.action`, write no file, omit `remediation.path`.
- **An object that already exists and has no declaration you can find is `kind: manual`.** Describe the change in `recommendation.action`, write no file, and omit `remediation.path`. Never invent a new path for it.
- `remediation.path` is relative to the repository root — which is `workspace`, not the directory you happen to be in — and must match the file you wrote exactly. No `..`, no glob metacharacter (`*`, `?`, `[`, `]`), no leading `:` — the helper rejects all of them.
- One file per remediation. Two findings share a path only where 2.7 says so (the per-namespace `default` ServiceAccount).
- **Findings that share a path share a Pull Request.** The promotion unit is the group of findings whose `remediation.path` values intersect, unioned transitively. 2.7 is the one case in this SOP that produces a group: every finding in a namespace points at that namespace's single `default` ServiceAccount declaration, so all of them are one group, on one branch, in one PR. Every other check here is one finding, one path, one PR.
- Manifests are proposals. Never `kubectl apply` them and never embed a live `resourceVersion`.
- For `kind: gcloud` and `kind: manual`, write no file and **omit `remediation.path` entirely** — the helper rejects a path on a non-manifest remediation. Put the full command or ordered human steps in `remediation.note`, with real cluster, location, project, and object names substituted — no angle-bracket placeholders except the human-supplied CIDR in 2.10. Neither kind is ever promotable to a PR; a `/remediate` request naming one is refused.
- A `kind: gcloud` `note` is rendered into the ledger issue **inside a bash fence**, so it must be shell-pasteable: commands on their own lines, and caveats (2.8 and 2.9 both recreate nodes; 2.10 needs a human-supplied CIDR) as `#` comment lines above the command they guard. Prose in a `gcloud` note renders as broken shell. A `kind: manual` note is rendered as prose and should read as prose.

### 4. Emit findings.json

Write the whole document to `findings_path` in one shot, with `audit: "compliance-audit"`, `scope.clusters` listing every cluster you queried — each carrying the `checks_run` list §1 required and, where §1 recorded them, that cluster's `checks_not_applicable` entries and `limitations` string — and `scope.skipped` listing only the clusters you could not read. Self-check before writing:

- Every cluster carries a non-empty `checks_run` of `{check, command}` objects naming the §2 checks that actually ran there and the commands that ran them. Never write the full eleven because the SOP lists eleven — write the ones you ran. An inflated `checks_run` is the one entry in this document that converts a partial audit back into a false all-clear, which is the exact failure the field exists to prevent, and a fabricated `command` is that lie published verbatim in a public issue. `checks_not_applicable` is the same lie wearing a different field: it removes checks from the denominator, so a slug parked there because you ran out of turns is a coverage gap the ledger will never show. It is published too — every exclusion and its reason render under _Not applicable_, where a reviewer who knows the cluster can call it. An honest seven-of-eleven costs you nothing but an open ledger, and an honest seven-of-seven on a cluster where the other four cannot apply closes it.

- Every finding has a non-empty `evidence.command` that is the literal command run. Drop anything else.
- Every finding carries `check`, set to the slug in its check's `####` heading. No finding carries an `id`; the harness derives it (§2) and discards anything you write there.
- `namespace` is `""` for cluster-scoped findings (2.4, 2.5, 2.8, 2.9, 2.10); `object` is `<Kind>/<name>` (`Deployment/api`, `ClusterRole/dev-admin`, `NodePool/pool-1`, `Cluster/prod-usc1`) and names the object the check judged — for 2.5 the wildcard-bearing Role or ClusterRole, not its binding.
- Every finding carries a complete `recommendation` — see below.
- `remediation.path` is present iff `kind == "manifest"` and that file exists on disk.
- No cluster appears in both scope lists, and no finding names a cluster in `scope.skipped`. The validator rejects the document on either. A `limitations` note suppresses nothing: findings from the checks that _did_ run on that cluster belong in the file.

Emit the complete set of findings. The harness bounds the rendering, not you: it caps the issue body at 60,000 characters, trims each excerpt to 40 lines / 2,000 chars and each command to 2,000 chars, and caps the scope tables at 60 rows. When findings do not fit, the body says so and the title's counts remain the true totals — so trim `evidence.excerpt` to the lines that prove the finding rather than pasting a dump, and never drop a real finding to keep the ledger short.

**`recommendation` — required on every finding.** Three non-empty strings, no exceptions, on `gcloud` and `manual` findings that will never become a PR just as much as on promotable ones. You write it now because the evidence is in front of you now; deferring it to the moment a human asks for the fix is how the reasoning gets lost.

- `action` — what to do. Imperative, one or two sentences.
- `rationale` — why **this** fix and not the obvious alternative. Name the alternative you considered and say why you rejected it.
- `risk` — what breaks when it is applied, and the read-only check to run first.

Worked example, for a 2.6 finding on the `payments` namespace:

```json
"recommendation": {
  "action": "Apply a default-deny NetworkPolicy to the payments namespace.",
  "rationale": "Namespace-scoped default-deny is the smallest change that closes east-west exposure without touching mesh config; a mesh AuthorizationPolicy would also work but takes effect only for injected pods.",
  "risk": "Any unlabelled cross-namespace traffic into payments breaks on apply. Enumerate what currently reaches it first with `kubectl get svc,endpoints -n payments`, and land the per-service allow rules in the same change."
}
```

Three `rationale`/`risk` pairs in this SOP are check-specific and must not be written generically: 2.8 and 2.9 both recreate nodes, so say so in `risk`; 2.10's `risk` must state that an incomplete CIDR list locks every operator out of the API server, which is why the list comes from a human.

### 5. Close the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py finish --audit compliance-audit \
  --findings-file /opt/data/scratch/findings_compliance-audit.json
# -> {"status":"CLEAN"|"OPENED"|"UPDATED","issue_url":...,"new":n,"resolved":m,
#     "prs_opened":[...],"prs_closed":[...],"partial":false,"coverage_gaps":[],
#     "silent_ok":true}
```

`finish` owns publication end to end. Tier 1 is one ledger issue for this stream, rewritten in place every run and labelled `agent:audit`, `audit:compliance-audit`, `severity:<highest>`; a clean run closes it as completed. Tier 2 is a narrow remediation PR per remediation group, branched `platform-agent/fix-compliance-audit-<slug>-<digest>` off `main` — the digest is taken over the group's sorted remediation paths, so the branch is keyed on the files the fix touches and stays put across runs even though the finding ids are re-derived every morning — linked with `Part of #<issue>` and additionally labelled `audit:remediation`. A PR opens automatically only for a finding that is `critical` **and** `manifest` **and** has no **live** pull request on its branch: one the harness closed itself carries `audit:stale-closed` and may be promoted again, while one a human closed and one that merged may not, because re-opening either would overrule a person every morning. Capped at five per run, with any withheld findings named in the ledger. Everything else waits for a repo writer to comment `/remediate <finding-id>` or `/remediate all`. A comment naming ids arrives as `pending_remediation_requests` on the next run's `start`, so you know which manifests to write while inspecting; `all` does not, because it names no particular file — it is expanded at `finish` against that run's manifest findings. Every such comment gets exactly one answer on the ledger — an acknowledgement naming each target and what became of it, or a single refusal saying why (the commenter has no write access, an id is not in the current document, the target is not a `manifest`, or the command was not written at the start of its own line and so was never parsed as one) — so a standing request is answered once, not re-answered every run.

**Partial coverage.** `partial` is `true` when this run cannot speak for the whole fleet: anything in `scope.skipped`, or any cluster carrying a `limitations` string, and `coverage_gaps` names each one in a readable sentence. It constrains what the run is allowed to conclude, because a finding absent from a cluster you never read is not a finding that was fixed. So a partial run reports `resolved: 0` and posts no resolved-delta, closes no remediation PR as stale, and leaves the ledger open even with zero findings — `status` is still `CLEAN`, but the issue survives with a comment naming the gaps. A check declared in `checks_not_applicable` is not a gap and does not raise the flag: it left the denominator, so an Autopilot cluster that ran every check that _can_ apply to it is a fully covered cluster. That is the difference between a fleet whose ledger can close and one where two of three clusters are permanently ⚠. Keep the `limitations` string for what it means — a check that could have run here and did not. The flag means coverage and only coverage — it is `true` if and only if `coverage_gaps` is non-empty. §4's body budget dropping findings from the description does not raise it: those findings were seen, the title's counts include them, and the body names what it dropped.

**No finding this SOP produces meets the auto-promotion bar.** Every `manifest` check here is `major` or `minor` (2.6, 2.7, 2.11) and every `critical` check is `gcloud` or `manual`, so every remediation PR from this stream is human-requested. That is deliberate: the fixes worth shipping unattended are the ones a reviewer can merge without a conversation, and none of these are. Never inflate a severity to force a PR open.

**`silent_ok` decides silence. Do not re-derive it.** `finish` returns `silent_ok: true` only when this run moved nothing an operator needs to hear about: nothing new, nothing resolved, no coverage gap, no remediation PR opened or closed. Read the flag rather than reassembling the conditions from `status`, `new`, `resolved`, and `partial` yourself — that arithmetic is where a run talks itself into silence it has not earned. Two rules, and they are the whole rule:

- On a **scheduled** run, `silent_ok: true` → your final response is exactly `[SILENT]`. Otherwise report, and every report carries `issue_url` in full.
- **An on-demand run is never silent.** If a person dispatched this job — from a kanban card, from chat, from `cronjob(action='run')` — someone is waiting on the answer, and `[SILENT]` throws it away. Report the outcome and the ledger URL whatever `silent_ok` says.

What to report in each case:

- `silent_ok: true` → `[SILENT]` on a scheduled run. No preamble, no "no issues found"; a clean fleet is a silent fleet. On `CLEAN` the ledger issue closed as completed; on `UPDATED` nothing moved and the ledger already says everything you would. Dispatched on demand, say so in one line and give the issue URL.
- `status == "CLEAN"` with `resolved: > 0` → the fleet was carrying findings and is not any more. Report it: the issue URL, and how many findings closed with it. This is the one piece of good news the audit produces, and swallowing it while reporting every failure teaches the operator that the audit only ever brings problems.
- `status == "CLEAN"` with `partial: true` → the ledger stayed open because the run could not see the whole fleet: one line reporting the clean result and `coverage_gaps`, then stop.
- `status == "OPENED"`, or `"UPDATED"` with a non-zero `new` or `resolved` → one line, then stop: `Security & RBAC posture audit: <new> new, <resolved> resolved across <count(scope.clusters)> clusters — <issue_url>`
- Exit 2 means the validator rejected the document and nothing was published: fix the findings file and re-run `finish`. Exit 1 is fatal. Exit 0 published. Do not work around the validator, and never open the issue or a PR by hand.
- A finding that still reproduces after its remediation PR merged renders in the ledger with a `⚠ fix merged, still reproduces` warning and the merged PR gets one comment. The audit never reopens it, and neither do you — re-verify the finding and let the next run carry it.

## Red Lines

- **Read-only.** No `kubectl apply|patch|create|delete|edit|scale|exec|run|port-forward|cp`, no `gcloud container clusters|node-pools update`, no write of any kind against any cluster. `gcloud container clusters get-credentials` is the sole exception and touches only a local kubeconfig.
- **No hand-written issue body, PR body, branch, commit, or `gh` call.** `audit_report.py` owns the entire git/GitHub path. Never call `gh issue create` — one stream has one ledger and `finish` owns it — never open a remediation PR yourself, and do not invoke `submit-suggestion` from this SOP.
- **Never comment on the ledger yourself.** `/remediate` is a human reviewer's instruction to this harness, not a step in the audit: an agent that posts it — including when someone asks for a fix in chat — is authorizing its own pull request. `finish` ignores a `/remediate` from a machine account, so posting one achieves nothing but noise on the issue.
- **No unreproducible findings.** No `evidence.command`, no finding. Never soften something you could not verify into a lower severity or a "possible issue" — delete it.
- **No finding without a `recommendation`.** All three sub-fields, non-empty, on every finding, written while the evidence is still in front of you.
- **No unstable identity.** The id is derived, so the way to destabilise it is to write an unstable `object` — a pod name with its ReplicaSet suffix, a generated resource name, the binding one run and the role the next. Name the durable object the check judged and audit the owning controller, never the pod. A finding whose identity moves is reported as fixed and re-reported as new, on a ledger people trust to tell them what is still broken.
- **No inference from an unaudited cluster.** A cluster you could not read goes in `scope.skipped` and never appears in a finding. A cluster you read where some checks did not run stays in `scope.clusters`, with Autopilot's 2.1–2.3 and 2.9 in `checks_not_applicable` and anything a later run could still clear — a command that errored, an absent API group — in `limitations`. Never demote a partially-checked cluster to `scope.skipped`: that silently discards every real finding from the checks that did run on a cluster you were told to audit.
- **No forbidden sources.** No BigQuery, Prometheus, Policy Controller / Gatekeeper, Security Command Center, external blueprint, or CMDB — and no kanban delegation to Cluster Agents. This audit runs entirely in the Platform Agent.
- **Never print raw credentials.** ServiceAccount tokens, kubeconfig contents, Secret `data:` blocks, and private keys never appear in `evidence.excerpt` — record the object reference, or re-run with a field selector or `-o jsonpath` that omits the value. The harness's redaction is a backstop, not permission.
