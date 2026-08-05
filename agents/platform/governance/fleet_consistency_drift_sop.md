# SOP: Fleet Consistency Drift Audit (Weekly Governance)

**Purpose:** Find the cluster that does not match its peers. For each configuration facet, compute what the majority of comparable clusters do, then report the outliers — answering "eleven of my twelve clusters have Binary Authorization on; which one doesn't, and why didn't I know?"

**Data sources:** The baseline is **derived from the live fleet and nowhere else.** `gcloud container clusters list/describe --format=json`, `gcloud container node-pools list/describe --format=json`, read-only `kubectl`, the `gke` MCP server, and the platform MCP tools (`list_cc_pods`, `get_cc_pod_diagnostics`, `list_cc_healthchecks`, `get_cc_operator_status`). There is no Platform Master Blueprint, no standards document, no CMDB, no Terraform state, no Config Sync repo, no BigQuery, no Prometheus. If you find yourself wanting an "expected value" from outside the fleet, you have left this SOP.

---

## Execution Checklist

### 0. Open the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py start --audit fleet-consistency-drift
```

Returns `{"issue":…, "repo":…, "workspace":"/opt/data/gitops/fleet-consistency-drift/<owner>__<name>", "findings_path":"/opt/data/scratch/findings_fleet-consistency-drift.json", "pending_remediation_requests":[…]}`. Use the returned `findings_path` verbatim. `workspace` is the GitOps clone `start` made — the pod has no checkout of its own — and any `remediation.path` you emit in §5 is resolved against it. `issue` is this stream's open ledger issue, or `null` when it has none; `finish` opens or rewrites it either way. There is no audit branch and no report branch: do not create branches, commit, push, or call `gh` yourself — the helper owns every git and GitHub operation and renders the ledger issue body. You never hand-write it.

`pending_remediation_requests` names findings a repo writer asked for by commenting `/remediate <finding-id>` on the ledger; write the manifest for each during inspection. Only `manifest` remediations are promotable and this audit emits almost none (§5), so the list is normally empty — the helper refuses a request naming a `gcloud` or `manual` finding with a comment of its own. Every request gets exactly one reply, whichever way it goes: an acknowledgement naming each target and its outcome, or a single refusal with the reason (no write access, an id absent from the current document, a non-`manifest` target, or a command not written at the start of its own line and so never parsed as one), written once behind a hidden marker rather than repeated each run. Never invent a manifest to satisfy one.

### 1. Enumerate the target fleet

1. Resolve the project set: `gcloud config get-value project`, plus any project IDs already recorded in `/opt/data/INVENTORY.md`. `INVENTORY.md` supplies **project IDs only** — never expected values.
2. Per project, enumerate with `gcloud container clusters list --project <proj> --format=json`, which returns full Cluster resources.
3. For every enumerated cluster capture the authoritative JSON with `gcloud container clusters describe <name> --location <loc> --project <proj> --format=json`. That literal invocation, with real values, is the `evidence.command` of every finding about that cluster. Never record a command you did not run.
4. **The one-question scope rule.** A cluster appears in exactly one scope list. Could you read it? Yes → `scope.clusters`; if some facets were not compared there, split them — the ones you could have compared and did not go in that cluster's `limitations`, the ones its shape rules out in `checks_not_applicable`. No → `scope.skipped`. Nothing goes in both, and nothing in `scope.skipped` may appear in a finding. The validator enforces all three.

   **Each `scope.clusters` entry is `{name, location, project, checks_run}`, and `checks_run` is mandatory.** Each entry in it is an object, never a bare string:

   ```json
   {
     "check": "shielded-nodes",
     "command": "gcloud container clusters describe prod-usc1 --location us-central1 --project acme-prod --format='value(shieldedNodes.enabled)'"
   }
   ```

   `check` is the backticked slug of the §4 facet you compared — `release-channel`, `shielded-nodes`, `datapath-provider`, and so on — never the section number and never prose. There are nineteen; 4.2 and 4.10 are owned by other streams and are not among them. (`start` prints the full roster; the SOP still says what each facet _is_ and how its cohort is drawn.) `command` is the literal invocation you issued to read that facet on that cluster. The step-3 `describe` is the honest answer for most of them, written out with real values — the same command may legitimately back several facets, since one `describe` is where you read them. It must name one of `kubectl`, `gcloud`, `gsutil`, `bq`, `helm`, or `curl`; `echo`, `cat`, `python3 -c`, and a call back into `audit_report.py` are all rejected.

   The validator rejects an unknown slug, a duplicate, a missing or unusable command, and the field being absent: a cluster you read but compared nothing on is not a clean cluster, it is an audit that did not happen. Anything short of the facets that apply to that cluster makes the run **partial**, so the ledger stays open and nothing is announced as resolved. Every command is published verbatim in the ledger under _How this run checked the fleet_, so paste what you ran rather than reconstructing it afterwards.

   **A facet the cluster's shape rules out is not a gap — declare it.** Alongside `checks_run`, a cluster may carry `checks_not_applicable` as a list of `{check, reason}`:

   ```json
   {
     "check": "image-type",
     "reason": "GKE Autopilot: Google selects the node image; there is no per-cluster setting to compare."
   }
   ```

   Same slugs as `checks_run`, and the `reason` must say why the facet _cannot_ apply here — "N/A" and "not applicable" are rejected; name the property of the cluster that rules it out. Those facets leave the denominator instead of counting as missing, so an Autopilot cluster — which has no user-managed pool behind the five §4 facets marked _Standard cohorts only_ (`secure-boot`, `integrity-monitoring`, `pool-autoscaling`, `node-autoprovisioning`, `image-type`) — reads as complete at fourteen of fourteen rather than forever-incomplete at fourteen of nineteen, and the ledger can eventually close. Use it only for facets the cluster's shape rules out. A facet you could have compared and did not is a `limitations` note and a real gap, and the validator rejects a slug in both lists, a duplicate, an unknown slug, and a reason under sixteen characters.

   A cluster excluded from every cohort is the one case where `checks_run` is legitimately `[]` — nothing was compared, because there was nothing to compare it against. That is allowed **only** alongside the `limitations` string below saying so, which is what keeps an unexplained empty list from reading as a clean cluster. That is a coverage gap, not an inapplicability: the facets exist here, they simply had no peer to compare against, so it stays in `limitations`.

   So `scope.clusters` is **every cluster you read**, compared or not — the harness rejects an empty list. If **zero** clusters enumerate, do not call `finish`: a fleet you could not read is not a clean fleet, so report the enumeration failure as your one-line summary and stop rather than returning `[SILENT]`.

   `scope.skipped` holds one case and one only: `describe` failed or was denied — quote the error in the reason. A cluster you read but did not compare stays in `scope.clusters` and says so in its `limitations`:
   - `status` is not `RUNNING` (`PROVISIONING`, `RECONCILING`, `STOPPING`, `ERROR`, `DEGRADED`) — a cluster mid-change is not drifting. `"status RECONCILING: excluded from every cohort, no facet compared."`
   - `createTime` is under 24 hours old — a brand-new cluster has not settled. `"created <createTime>: under 24h, excluded from every cohort."`
   - Its cohort is below the §2 floor — see §2.4 for the wording.

### 2. Build comparability cohorts

A dev cluster diverging from a prod cluster is intent, not drift. Group before comparing.

1. **Mode** (always part of the key): `autopilot` when `.autopilot.enabled == true`, else `standard`. Autopilot and Standard clusters are **never** comparable for node-level facets and are kept in separate cohorts throughout.
2. **Environment signal**, resolved in this fixed order: the first present of `.resourceLabels.environment`, `.env`, `.stage`, `.tier`, lowercased; otherwise a token match in the cluster name split on `-`/`_` against `{prod, prd, production, staging, stg, stage, preprod, dev, development, sandbox, sbx, test, qa, uat}`, which yields an **inferred** environment and costs a severity step later; otherwise `unknown`. Normalize synonyms: `prod|prd|production → prod`, `staging|stg|stage|preprod → staging`, `dev|development|sandbox|sbx → dev`, `test|qa|uat → test`. Any other literal keeps its own value.
3. **Cohort key:** if any cluster in the fleet has a non-`unknown` environment → `(mode, environment)`, and the `unknown` clusters form their own cohort, never merged into a named one. Else if the fleet spans more than one project → `(mode, project)`, using the project as the environment proxy. Else → `(mode)` alone.
4. **Minimum cohort size — the floor.** A cohort of fewer than **3** clusters produces no findings, ever; two clusters disagreeing is a coin flip, not a majority. You read every member of an undersized cohort, so they stay in `scope.clusters`; record the exclusion in each member's `limitations` as `cohort <key> has only N comparable clusters (minimum 3), no facet compared`. If no cohort reaches 3, the run emits zero findings and still completes §6 and §7 — which only works because a floored-out fleet still has a non-empty `scope.clusters`.

### 3. Derive the baseline

For each cohort `C` (size ≥ 3) and each facet `F`:

1. Normalize every member's raw value to one comparable token per the facet's rule. A member whose value cannot be read (field absent fleet-wide, API error, permission denied) is `UNREADABLE`: excluded from the vote, and never an outlier for that facet. The cluster itself was read, so it stays in `scope.clusters`; when the cause is an API error or a permission denial rather than a genuinely absent field, name the facet in that cluster's `limitations`.
2. Let `n` = voting members, `m` = count of the most common token `t*`, `r = m / n`, `k = n - m` outliers.
3. **A baseline exists only when `n >= 3` and `r >= 2/3`.** Otherwise there is no baseline and no finding — reporting a 50/50 or a 4/7 split as drift is noise. (A tie for first place cannot reach `r >= 2/3`, so uniqueness of `t*` follows.)
4. Every member whose token differs from `t*` is an outlier and yields one finding.
5. **Confidence to severity.** Start at the facet's base severity and walk down the ladder `critical > major > minor`: `r < 0.90` → one step; `r < 0.80` → one further step (cumulative); `k >= 3` → one step (three-plus divergent clusters is an undeclared cohort, not an outlier); the outlier's or the baseline's cohort membership rests on an **inferred** environment → one step. If the result falls below `minor`, **drop the finding.** A base-`major` facet at `r = 0.71` therefore disappears while a base-`critical` facet at `r = 0.71` survives as `minor`. That is intended: a weak majority only earns an admin's attention when the stake is high.
6. **Split-cluster guard.** If one cluster is an outlier on **6 or more** facets it is not drifting, it is a different kind of cluster. Suppress its individual facet findings and emit one `major` finding with `check: uncohorted` against that cluster, naming them, so the admin fixes the cohort labelling instead of twelve symptoms.
7. **Identity is derived — do not write an `id`.** The harness builds it from `check`, `cluster`, `namespace` and `object`, and ignores any `id` in the file. Set `check` to the slug backticked in the facet's §4 heading — a section covering several facets backticks one slug per facet, in the order its **Read** bullet lists them — and `object` to `Cluster/<name>`. The split-cluster finding in step 6 uses `check: uncohorted`; it is the one slug here that is not a facet, and no cluster is ever asked to run it. Never let counts, ratios, dates, or severities into any of the four: the delta depends on the same drift keeping the same identity between runs, and drift that changes identity is announced as fixed.
8. **Every finding shows its work.** `evidence.excerpt` opens with these four labelled lines — the harness clips excerpts at 40 lines / 2000 characters, so they go first and the raw JSON fragment follows. Without them the audit reads as an oracle and gets ignored:

```
baseline: <field path>=<t*> in <m>/<n> clusters of cohort <mode>/<env>
peers: <up to 6 cluster names>, +<N> more
observed: <token>  (<raw JSON fragment or "key absent">)
consensus: <r to 2dp> -> severity <sev> (base <base>, <downgrades applied or "none">)
```

### 4. Facet comparison

#### 4.0 Rules that apply to every facet

- **Field-path discipline.** Confirm a facet's path exists in at least one cluster's real `--format=json` output before comparing it. If it is absent from every cluster in the cohort the facet is `UNREADABLE` fleet-wide — skip it silently; never emit a finding asserting the whole fleet is missing a field you could not locate. Where two paths are plausible (a field that migrated between API versions), read the first present and record which one in the excerpt.
- **Absent, empty, and `false` are one token.** Proto3 omits false booleans and empty messages, so a missing `shieldedNodes` key, `{"shieldedNodes":{}}`, and `{"enabled":false}` all normalize to `OFF`. A serialization artefact must never become a finding.
- **Baseline** is the plain §3 majority over the cohort unless a facet says otherwise.
- **Global suppressions — do NOT flag** (assumed everywhere below, not repeated): clusters excluded in §1, whether unreadable and therefore in `scope.skipped` or read-but-not-compared and therefore carrying a `limitations` string; clusters in a different cohort; Autopilot clusters on any node-level facet; `UNREADABLE` values; cohorts under the §2 floor; facets with no §3 baseline. An Autopilot cluster is never skipped — you read it — so it stays in `scope.clusters` and names the five node-level facets that do not apply to it (`secure-boot`, `integrity-monitoring`, `pool-autoscaling`, `node-autoprovisioning`, `image-type`) in its `checks_not_applicable`, one entry each. **Not in `limitations`:** that string is the coverage flag, so a mode note in it would report a fully compared cluster as partially compared on every run there is, and pin the ledger open for as long as the cluster stays Autopilot.

#### 4.1 Release channel (`release-channel`)

- **Read:** `.releaseChannel.channel` → `RAPID`/`REGULAR`/`STABLE`. A cluster reading absent, `{}`, or `UNSPECIFIED` is **not enrolled**; drop it from the vote as `UNREADABLE` and emit nothing for it — enrolment is owned by the Upgrade & Patch Readiness audit (`security-patch-orchestrator` check 3.4), which flags it absolutely and does not need a majority to be right. This facet compares only clusters that are on a real channel, and reports the one sitting on a different real channel from its cohort.
- **Do NOT flag:** an unenrolled cluster, per the read rule above; a cluster pinned to a specific `currentMasterVersion` for a documented dependency — check `.resourceLabels` for a pin/freeze marker the fleet uses elsewhere first.
- **Severity:** base `minor` — a mismatch between two real channels is a rollout-cadence difference, not an absent control.
- **Impact:** the outlier receives security patches on a different schedule than every peer.
- **Remediation:** `gcloud` — `gcloud container clusters update <name> --location <loc> --project <proj> --release-channel=<t*>` (`# changing channel can move the cluster's version; downgrading a channel is not always permitted`).

#### 4.2 Workload Identity — owned by the Security & RBAC Posture Audit

**Not compared here. Emit no finding for it, and give it no facet slug.** Workload Identity being off is a defect against an absolute standard, not against a cohort, and the Security & RBAC Posture Audit (`compliance-audit` check 2.8) already reports it that way on every cluster in the fleet. A majority vote is strictly weaker: a cohort that has Workload Identity off everywhere produces no drift finding at all, and every cluster in it is still wrong. Reporting it in both places would put one cluster's single misconfiguration in two ledgers with two remediation notes.

#### 4.3 Shielded Nodes, secure boot, integrity monitoring (`shielded-nodes`, `secure-boot`, `integrity-monitoring`)

- **Read:** three facets. Cluster-level `.shieldedNodes.enabled` → `ON`/`OFF`. Per-pool `.nodePools[].config.shieldedInstanceConfig.enableSecureBoot` and `.enableIntegrityMonitoring` → `ALL` (every pool true), `SOME` (mixed), `NONE` (no pool true, or absent on all pools). Standard cohorts only for the two per-pool facets.
- **Do NOT flag:** pools whose `.config.imageType` cannot support secure boot (Windows and some third-party images) — exclude those pools from the fraction rather than letting them manufacture a `SOME`; a cluster whose only divergent pool is a burst or spot pool created in the last 24 hours.
- **Severity:** base `major` for Shielded Nodes and secure boot, base `minor` for integrity monitoring.
- **Impact:** nodes boot unverified where every peer verifies them.
- **Remediation:** `gcloud` for the cluster flag — `gcloud container clusters update … --enable-shielded-nodes`. Per-pool secure boot cannot be toggled in place, so those are `manual`: recreate the pool with `--shielded-secure-boot`.

#### 4.4 Network policy enforcement (`network-policy`)

- **Read:** if `.networkConfig.datapathProvider == ADVANCED_DATAPATH` → `DPV2` (Dataplane V2 enforces natively and the Calico fields are meaningless there); else if `.networkPolicy.enabled == true` and `.addonsConfig.networkPolicyConfig.disabled` is not true → `CALICO`; else `OFF`. Confirm both Calico paths in the real JSON before relying on either.
- **Do NOT flag:** `DPV2` against a `CALICO` majority or the reverse — two implementations of one control. Emit a finding only when the outlier is `OFF`.
- **Severity:** base `major`.
- **Impact:** pod-to-pod traffic is unrestricted in the outlier where peers segment it.
- **Remediation:** `gcloud` — `gcloud container clusters update … --enable-network-policy` (`# restarts the cluster networking add-ons`).

#### 4.5 Private nodes, private endpoint, authorized networks (`private-nodes`, `private-endpoint`, `authorized-networks`)

- **Read:** three facets, each `ON`/`OFF`, from `.privateClusterConfig.enablePrivateNodes`, `.privateClusterConfig.enablePrivateEndpoint`, and `.masterAuthorizedNetworksConfig.enabled` plus `.cidrBlocks` (authorized networks is `ON` only when enabled **and** `cidrBlocks` is non-empty). Recent GKE versions carry equivalents under `.networkConfig` and `.controlPlaneEndpointsConfig`: read whichever is actually present and name the path in the excerpt.
- **Do NOT flag:** the **contents** of `cidrBlocks` — CIDRs legitimately differ per cluster and comparing them is guaranteed noise; `enablePrivateEndpoint: false` when the majority is also false (normal for admin-reachable control planes).
- **Severity:** base `critical` for private nodes and authorized networks, base `major` for private endpoint.
- **Impact:** the outlier exposes node or control-plane surface its peers keep private.
- **Remediation:** authorized networks are `gcloud` — `gcloud container clusters update … --enable-master-authorized-networks --master-authorized-networks=<ranges the cluster's own owner approves>`; never copy a peer's CIDRs. Private nodes usually cannot be enabled in place: prefer `manual` with the migration note unless `--enable-private-nodes` is valid for that cluster's version.

#### 4.6 Logging and monitoring component sets (`logging-components`, `monitoring-components`, `managed-prometheus`)

- **Read:** three facets. `.loggingConfig.componentConfig.enableComponents[]` and `.monitoringConfig.componentConfig.enableComponents[]` are list-valued: deduplicate, sort ascending lexicographically, join with `,` — that canonical ordering is what makes them comparable, and an absent config, an absent `enableComponents`, and an empty list all become `NONE`. `.monitoringConfig.managedPrometheusConfig.enabled` → `ON`/`OFF`.
- **Do NOT flag:** a cluster whose set is a strict **superset** of the baseline — collecting more telemetry than peers is not drift. Flag subsets and disjoint sets, and name the missing components.
- **Severity:** base `major` when the outlier is missing `SYSTEM_COMPONENTS`, otherwise base `minor`.
- **Impact:** the outlier is invisible to fleet dashboards and alerts built on the peers' component set.
- **Remediation:** `gcloud` — `gcloud container clusters update … --logging=<t* comma list> --monitoring=<t* comma list>`.

#### 4.7 Binary Authorization (`binary-authorization`)

- **Read:** `.binaryAuthorization.evaluationMode`, falling back to legacy `.binaryAuthorization.enabled` when absent. `DISABLED`, `EVALUATION_MODE_UNSPECIFIED`, an absent block, and legacy `enabled: false` all → `OFF`.
- **Do NOT flag:** mode differences among enabled clusters — the policy content lives outside the cluster and is unreadable here, so only `OFF` against an enabled majority is a finding.
- **Severity:** base `major`.
- **Impact:** unsigned or unattested images can run on the outlier.
- **Remediation:** `gcloud` — `gcloud container clusters update … --binauthz-evaluation-mode=<t*>` (`# the project policy must already admit this cluster's workloads or deployments will be blocked`).

#### 4.8 Cluster autoscaling and node auto-provisioning (`node-autoprovisioning`, `pool-autoscaling`)

- **Read:** two facets, Standard cohorts only. `.autoscaling.enableNodeAutoprovisioning` → `ON`/`OFF`; `.nodePools[].autoscaling.enabled` → `ALL`/`SOME`/`NONE` as in §4.3.
- **Do NOT flag:** single-pool clusters against multi-pool peers on the `ALL`/`SOME`/`NONE` facet; pools carrying `.config.taints` that mark them dedicated or pinned capacity — exclude those from the fraction, they are deliberately fixed-size.
- **Severity:** base `minor`.
- **Impact:** the outlier cannot absorb load the way its peers do and needs manual capacity intervention.
- **Remediation:** `gcloud` — `gcloud container node-pools update <pool> --enable-autoscaling --min-nodes=<N> --max-nodes=<N>` or `clusters update … --enable-autoprovisioning …`; the limits are a human judgement, so leave them as named placeholders in a `#` comment rather than inventing numbers.

#### 4.9 Intra-node visibility and dataplane provider (`intra-node-visibility`, `datapath-provider`)

- **Read:** two facets. `.networkConfig.enableIntraNodeVisibility` → `ON`/`OFF`; `.networkConfig.datapathProvider` → `ADVANCED_DATAPATH`, or `LEGACY_DATAPATH` for both the explicit value and an absent field.
- **Do NOT flag:** the dataplane facet on Autopilot cohorts — Autopilot is always Dataplane V2, so variation there is a read error, not drift.
- **Severity:** base `minor` for intra-node visibility, base `major` for dataplane provider.
- **Impact:** the outlier emits different flow telemetry and enforces network policy through a different engine than its cohort.
- **Remediation:** intra-node visibility is `gcloud` — `gcloud container clusters update … --enable-intra-node-visibility`. Dataplane V2 cannot be enabled on an existing cluster, so that one is `manual`: cluster recreation and workload migration.

#### 4.10 Maintenance window — owned by the Upgrade & Patch Readiness Audit

**Not compared here. Emit no finding for it, and give it no facet slug.** The only divergence this facet could ever report is a cluster with **no** window against a configured majority — `DAILY` against `RECURRING` is not drift, and start times are incomparable across regions, since a window at 02:00 local in `us-east4` is a business-hours window in `asia-northeast1`. "No maintenance window" is exactly what the Upgrade & Patch Readiness audit (`security-patch-orchestrator` check 3.7) reports, absolutely and on every cluster. Nothing is left for a majority vote to add.

#### 4.11 Resource label key set (`label-keys`)

- **Read:** `.resourceLabels`; token is the sorted set of **keys** joined with `,`, dropping keys prefixed `goog` (GKE writes those itself). Absent map and empty map both → `NONE`.
- **Baseline:** the majority key set. The expected keys are whatever the cohort demonstrably carries — **do not invent label keys** such as `owner` or `cost-center` because they sound standard.
- **Do NOT flag:** label **values** (they vary by design, including the environment label used for cohorting); a cluster carrying extra keys beyond the baseline; any key held by fewer clusters than the §3 threshold.
- **Severity:** base `minor`.
- **Impact:** the outlier drops out of cost attribution and label-scoped queries its peers appear in.
- **Remediation:** `gcloud` — `gcloud container clusters update … --update-labels=<key>=<VALUE>` (`# VALUE must be supplied by the cluster owner`).

#### 4.12 Node image type (`image-type`)

- **Read:** `.nodePools[].config.imageType`; token is the sorted set of distinct image types joined with `,`. Standard cohorts only.
- **Do NOT flag:** Windows pools in an otherwise Linux cluster — a deliberate workload requirement, visible as a `WINDOWS_*` image type, so exclude those pools from the set; a difference that is only a `_CONTAINERD` suffix on the same base family, which is a rename rather than a divergence.
- **Severity:** base `minor`.
- **Impact:** the outlier's nodes carry a different patch cadence, kernel, and hardening baseline.
- **Remediation:** `manual` — image type cannot be changed in place; recreate the pool with `--image-type=<t*>`.

#### 4.13 Database encryption, etcd CMEK (`database-encryption`)

- **Read:** `.databaseEncryption.state` → `ENCRYPTED`/`DECRYPTED`, with an absent block `DECRYPTED`. Compare the state only — **never `keyName`**, which is region- and project-scoped and legitimately differs.
- **Do NOT flag:** `ENCRYPTED` with an unreachable key — that is a health problem for a different audit, not consistency drift.
- **Severity:** base `critical`.
- **Impact:** application secrets in the outlier's etcd are not wrapped with the customer-managed key every peer uses.
- **Remediation:** `gcloud` — `gcloud container clusters update … --database-encryption-key=<KEY>` (`# KEY must be created in the cluster's region and IAM-bound by a human first; do not reuse a peer's key`).

### 5. Generate remediation artifacts

- These are control-plane settings, so `kind` is almost always `gcloud` or `manual`. For `gcloud` the harness renders `remediation.note` **inside a bash code block**, so the note must be shell-pasteable: the command, with any caveat as a `#` comment. For `manual` the note is prose — say plainly that the change needs pool or cluster recreation rather than emitting a command that would fail.
- `remediation.path` is **only permitted when `kind == "manifest"`**; a path on a `gcloud` or `manual` remediation is a hard validation failure. Use `manifest` only when the fix is a genuine in-cluster object. When it already exists, write the object's **complete** desired manifest over its existing declaration in the GitOps repo (`grep -rl "name: <object>" --include='*.yaml' <workspace>`) and give that file's path — never a patch fragment, which is not valid `kubectl apply` input, and never a second file for an object the repo already declares, which is a duplicate resource id to Config Sync and Argo. When the object does not exist yet, write it complete into the directory that already holds the applied declarations for the same cluster and namespace — discover that directory from a sibling (`grep -rl "namespace: <namespace>" --include='*.yaml' <workspace>`), **open the hits and confirm one declares an object you observed on the target cluster** before writing beside it, and match the naming style of the files already there. Never anchor on `grep "name: <object>"` alone: `grep` is kind-blind, so it also matches `app.kubernetes.io/name:` label lines and names that merely share a prefix, and it will return files under another cluster's directory. **Hits in more than one directory, or none tied to the target cluster, means `kind: manual`** — a namespaced object written into the wrong cluster's tree fails that tree's sync. **Never create a new top-level directory, and never write to a path whose parent directory does not already exist in the clone**: the repository is reconciled by a tool that applies a fixed set of paths, and a file outside them is applied by nothing — it merges clean, closes the finding for one run, and changes nothing on the cluster. If the object exists and you cannot find its declaration, or nothing in the clone declares that cluster and namespace at all, the finding is `kind: manual` with the change described in `recommendation.action`. Either way write the file into the §0 `workspace` **before** `finish` and give its path relative to that clone's root — the harness stages exactly those files and errors on a path that is absolute, contains `..`, carries a glob metacharacter (`* ? [ ]`), or starts with `:`. A path pointing at no file is the one case it forgives: that finding degrades to `kind: manual`, keeping its evidence and recommendation and stating in the ledger that the fix was named but never written, and the report publishes regardless.
- **Only a `manifest` finding can become a pull request.** A `critical` manifest finding opens a narrow remediation PR automatically, branched from `main` and linked to the ledger with `Part of #<issue>` — capped at five per run, with the withheld ones named in the ledger as awaiting `/remediate`, and only when its branch has no **live** pull request on it (a stale close by the harness carries `audit:stale-closed` and reopens the door; a human's close and a merge do not). Every other manifest finding waits for a repo writer to comment `/remediate <finding-id>`, and findings whose `remediation.path` values overlap are promoted together in one PR. Since this audit is `gcloud` and `manual` almost throughout, nearly every drift finding lives and dies as prose in the ledger issue. That is the intended outcome: a control-plane flag is a command to run, not a file to merge.
- Never copy cluster-scoped values from a peer (CIDRs, KMS key names, label values, autoscaling limits). Leave a named placeholder and say what the human supplies.
- Mark disruptive remediations in the note: anything that recreates node pools, restarts networking add-ons, or is one-way.

### 6. Emit findings.json

Write the document to the `findings_path` from §0, with `"audit": "fleet-consistency-drift"`.

- `scope.clusters` — every cluster you read, with `name`, `location`, `project`, and `checks_run` as `{check, command}` objects naming the §4 facets you actually compared for it and the commands you read them with, plus a non-empty `limitations` string wherever a cluster was read but not compared (§1.4, §2.4, §3.1); non-empty or the run fails. Pad a `checks_run` to nineteen because the SOP lists nineteen facets and you convert a partial audit into a false all-clear — the harness cannot see the comparison you skipped, so it believes you. The `command` is what makes that padding falsifiable rather than free: it is published verbatim, so an invented one is a false statement in a public issue. `checks_not_applicable` is the same lie wearing a different field: it removes checks from the denominator, so a slug parked there because you ran out of turns is a coverage gap the ledger will never show. It is published too — every exclusion and its reason render under _Not applicable_, where a reviewer who knows the cluster can call it. An honest sixteen-of-nineteen costs you nothing but an open ledger, and an honest sixteen-of-sixteen on a cluster where the other three cannot apply closes it.
- `scope.skipped` — only the clusters `describe` could not read, each with the quoted error (`[]` when nothing was skipped). No cluster may appear in both lists and no finding may name a skipped cluster; the validator rejects either.
- Per finding: `namespace` is `""` and `object` is `Cluster/<name>` for cluster-scoped facets; `check` per §3.7 and no `id` at all; `severity` one of `critical`/`major`/`minor`.
- `title` names the facet and the divergence and carries **no counts** — the harness renders the body deterministically, so a stable title keeps an unchanged fleet's findings section byte-identical between runs. (The body itself is not byte-identical: it carries generated timestamps.) Counts belong in the excerpt.
- `evidence.command` is **mandatory and must be the literal command you ran** — the §1.3 `describe` invocation with real values. A finding you cannot reproduce is dropped, not softened, not hedged, not reworded into a "possible" finding.
- `evidence.excerpt` leads with the four labelled lines from §3.8.
- **Credential hygiene:** never paste a Secret's `data:` block, a token, or a private key into `evidence.excerpt` — and on this audit that means never pasting `masterAuth`, which `clusters describe` returns with certificate and key material in it. Re-run the read with a field selector or an `-o jsonpath` / `--format` that omits the field and quote that instead. The harness redacts high-confidence credential shapes as a backstop, not as the primary control.
- `impact` is one non-empty sentence a platform admin can act on.
- Zero findings is a valid and common result: still write the file with the populated scope.
- Validate before publishing: `audit_report.py finish … --dry-run` renders the ledger issue and every PR body it would open, and checks everything, with no git or GitHub side effects.

**Every finding carries a `recommendation`.** Three sub-fields, all required, all non-empty, on **every** finding — not only the rare `manifest` one that could become a PR. Write it while the `describe` output is in front of you; deferring it until someone asks for the fix means writing it from memory.

- `action` — what to do, imperative, one or two sentences.
- `rationale` — why this fix and not the obvious alternative. Name the alternative you considered and say why you rejected it.
- `risk` — what breaks on apply, and the read-only check to run first. Control-plane flags are fleet-visible switches: say which workloads notice.

Worked example, for a 4.4 network-policy outlier:

```json
"recommendation": {
  "action": "Enable network policy enforcement on stg-eu-west with gcloud container clusters update stg-eu-west --location europe-west1 --project acme-stg --enable-network-policy.",
  "rationale": "Every other cluster in the (standard, staging) cohort enforces pod-to-pod policy, so this is the cohort's own baseline rather than an imported standard; migrating the cluster to Dataplane V2 instead would give the same control natively and with better performance, but that cannot be done in place and would mean recreating the cluster to close a gap a single flag closes.",
  "risk": "Enabling enforcement restarts the cluster's networking add-ons, and any namespace that already carries a NetworkPolicy starts enforcing it the moment the engine comes up. List what exists first with kubectl --context stg-eu-west get netpol -A."
}
```

### 7. Close the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py finish --audit fleet-consistency-drift \
  --findings-file /opt/data/scratch/findings_fleet-consistency-drift.json
```

- One JSON line comes back: `status`, `issue_url`, `new`, `resolved`, `prs_opened`, `prs_closed`, `partial`, `coverage_gaps`, and `silent_ok`.
- `partial: true` means the run could not compare the whole fleet — a cluster in `scope.skipped`, or one read but not compared and so carrying a `limitations` string (§1.4, §2.4, §3.1) — and `coverage_gaps` puts each gap in a sentence. On a drift audit that cuts deep, because the majority is what defines normal and a missing peer changes the majority. So the harness declines to infer: `resolved` comes back `0` with no resolved-delta, no remediation PR is closed as stale, and the ledger stays open even with an empty findings array — `status` is still `CLEAN`, the issue simply survives with a comment naming the gaps. A facet declared in `checks_not_applicable` is not a gap and does not raise the flag; it left the denominator. The flag tracks coverage and nothing else, so it is `true` exactly when `coverage_gaps` is non-empty; a body too long to hold every finding is a rendering limit, not a gap in what was compared, and says so in the body itself.
- **`silent_ok` decides silence. Do not re-derive it.** `finish` returns `silent_ok: true` only when this run moved nothing an operator needs to hear about: nothing new, nothing resolved, no coverage gap, no remediation PR opened or closed. Read the flag rather than reassembling that from `status`, `new`, `resolved`, and `partial` yourself — that arithmetic is where a run talks itself into silence it has not earned. Two rules, and they are the whole rule: on a **scheduled** run, `silent_ok: true` → your final response is exactly `[SILENT]`, otherwise report with `issue_url` in full; and **an on-demand run is never silent** — if a person dispatched this job, from a kanban card, from chat, or from `cronjob(action='run')`, someone is waiting on the answer and `[SILENT]` throws it away, so report the outcome and the ledger URL whatever `silent_ok` says.
- `silent_ok: true` → `[SILENT]` on a scheduled run. Nothing else, no summary, no preamble. On `CLEAN` the ledger issue closed as completed and any remediation PR still open for this stream closed with it; on `UPDATED` the fleet drifted no further and healed nothing. Dispatched on demand, say which in one line and give the issue URL.
- `CLEAN` with `resolved: > 0` → the fleet converged. Report the issue URL and how many drifts closed with it: a fleet that came back into line is worth a sentence, and it is the only good news this audit has to give.
- `CLEAN` with `partial: true` → nothing diverged among the clusters you could compare, and the ledger stayed open because that is not the whole fleet. One line with the result, the `coverage_gaps`, and the issue URL.
- `OPENED`, or `UPDATED` with a non-zero `new` or `resolved` → one line naming the audit, the `new` and `resolved` counts, the ledger issue URL, and anything in `prs_opened` / `prs_closed`.
- A schema violation exits 2 and publishes nothing; exit 1 is a fatal error and exit 0 is a publish. Fix the document and re-run `finish`. Never work around a validation error by deleting the finding that triggered it.

---

## Red Lines

- **Read-only.** Never run `gcloud container clusters update`, `node-pools update`, `kubectl apply`, `patch`, or `delete`. Remediations are text for a human, not commands to execute.
- **No external baseline.** No blueprint, standards document, CMDB, Terraform state, Config Sync repo, BigQuery, or Prometheus. If the fleet cannot tell you what normal looks like, there is no finding.
- **No delegation.** Do not create kanban cards for Cluster Agents; this audit reads the control plane itself and completes in one session.
- **No hand-written git or GitHub work.** `audit_report.py` owns the ledger issue, every remediation branch, commit, push, and pull request, and every body, label, and timestamp. Never call `gh issue create` or `gh pr create`; this stream has exactly one ledger issue and never a second.
- **Never comment on the ledger yourself.** `/remediate` is a human reviewer's instruction to this harness, not a step in the audit: an agent that posts it — including when someone asks for a fix in chat — is authorizing its own pull request. `finish` ignores a `/remediate` from a machine account, so posting one achieves nothing but noise on the issue.
- **No unreproducible findings.** If you cannot produce the literal command that shows the value, the finding does not exist.
- **No invented field paths, label keys, or standards.** Confirm every path against real `--format=json` output and derive every expectation from the majority.
- **No findings below the floor.** Fewer than three comparable clusters, or a consensus under two-thirds, means silence — an audit that calls a two-cluster disagreement "drift" gets switched off within a week, and then it protects nothing.
