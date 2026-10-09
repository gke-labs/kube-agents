# SOP: Upgrade Retrospective (Weekly Governance)

**Cron id:** `upgrade-retrospective` — `0 18 * * 0` (Sundays, 18:00 UTC). The Chat Agent's first-run stage also marks it due once, after the inventory scan settles.

**Purpose:** Report what the weekend's GKE upgrades did to the workloads on every managed cluster, so the next cluster does not meet the same failure. The other upgrade streams look ahead; this one looks back. For each cluster that is new to it, or that GKE upgraded since its last run, the collector records four things. (A) Which `UPGRADE_MASTER` and `UPGRADE_NODES` operations ran, from which version to which. (B) What stayed down, stuck or restarting afterwards, and which of the catalogue's twenty entries each symptom matches. (C) How to see the failure coming and how to fix it, written for the cluster's own objects. (D) The mitigation it set up. The collector records a guard in `guards.json`, for the readiness watch to read once it ships. Where a repository is linked, this stream's one ledger issue carries the finding. The run is **read-only**. It upgrades, rolls back, cordons, deletes and patches nothing, and it opens no pull request on its own.

**Data sources:** the collector `skills/fleet-audit/scripts/upgrade_retrospective.py`, which runs `gcloud container clusters list`, `gcloud container operations list`, `gcloud container get-server-config`, `gcloud container clusters get-credentials` and read-only `kubectl get`. Also `printenv GKE_PROJECT_ID`, the `list_cluster_profiles` tool for the roster, the collector's store and manifest on the shell volume, and the GitOps clone `start` hands you. **Nothing else.** No BigQuery, no Prometheus, no Cloud Logging, no external blueprint, no delegation to Cluster Agents via kanban. The collector classifies and grades. You verify the grade (section 3), tailor the fix (section 4), file the ledger (section 5) and write the chat lines (section 6).

---

## Execution Checklist

### 0. Which run this is, and the audit run

Two routes reach this SOP. The route decides the scope.

- **The scheduled tick and the first-run stage** are a **full run**. It reads the whole roster (section 1) and runs the collector with `--full` (section 2). It files the ledger issue where a repository is linked (section 5) and posts one chat line per reviewed cluster (section 6).
- **A question someone asked** is the on-demand route. "How did our upgrades go", "did anything break when the clusters were upgraded", "anything to fix before the next one", or a question that names a cluster. Read section 7 first. It decides whether the saved report answers the question or a run is needed. It also decides whether that run is full or scoped. Then apply the sections it names.

**Open the audit run before the collector, where a repository is linked.** `finish` refuses a manifest that finished before the run started (`manifest_predates_run`). So on a full run the order is `start`, then the collector, then `finish`. Read the managed repository list the way `SOUL.md` §1 says (the `managed_repos` key of `$GITOPS_STATE_CONFIGMAP`). With no GitHub entry, skip `start` and section 5: the report on the volume is the record, and section 6 says so in chat. A scoped run (section 7) never calls `start` or `finish`.

```bash
./skills/fleet-audit/scripts/audit_report.py start --audit upgrade-retrospective [--repo "<owner>/<repo>"]
```

If `managed_repos` names several repositories, pass `--repo "<owner>/<repo>"`. On a scheduled run, loop over all of them in sequence, with `start` and `finish` for each. On an on-demand full run, do the same unless the question named one. `start` returns `{"issue": <int|null>, "repo": "org/repo", "workspace": "...", "findings_path": "/opt/data/scratch/findings_upgrade-retrospective.json", "carried": [...], "pending_remediation_requests": [...], ...}`. Keep `findings_path` (section 2 drafts the document there), `workspace` (section 4 greps it for a manifest's path) and `carried` (the ids the previous run published). `start` also removes a manifest left at the path section 2 names, so a stale one cannot stand for this run. A `START REFUSED` line means a run of this stream is in flight, a scheduled tick's or another session's. Stop and say so; there is no override. A `BROKER UNAVAILABLE` line means the credential proxy did not answer: re-run the same command once it does. `start` creates and resets no branch. The helper owns all git and forge work and renders the ledger body itself. **Never hand-write a ledger body, never run `git commit` or a `vcs.py` verb yourself, and never comment on the ledger.**

One run at a time. The collector holds an exclusive lock on `.lock` under its store for the whole run. A second run prints one line (`another retrospective run holds … since …; nothing written`) and exits 0. When you see that line, do not retry and do not wait. On the scheduled route, report it as the run's one line and stop. On demand, answer from the latest saved report (section 7) and say a run is in progress.

### 1. The roster

The scheduled run reads the reconciler's roster: the management project, and every project a Cluster Agent profile is pinned to. It does not read every project the credential can list.

1. `printenv GKE_PROJECT_ID` is the management project (SOUL.md §1; never hardcode it and never pass `-` as a project).
2. Call the `list_cluster_profiles` tool. It returns one entry per Cluster Agent profile with `project`, `cluster` and `location`. A profile scaffolded without its identity has only `name` and contributes no project.
3. The `--project` set is the management project plus every distinct `project` from step 2. Pass a project named twice once.

Without `--project` the collector reads the active project plus everything `gcloud projects list` returns, which is more than the install manages. Pass the set every time, on the scheduled route and on a fleet-wide on-demand run.

### 2. Run the collector

```bash
python3 ./skills/fleet-audit/scripts/upgrade_retrospective.py --full --project "$GKE_PROJECT_ID" --project <each further project from section 1> --manifest-file /opt/data/scratch/manifest_upgrade-retrospective.json
```

Run it in the foreground, with a terminal timeout of at least 900 seconds, and never in the background. It reads every fleet cluster on a quiet week too, and re-reads every cluster that holds a guard. A second run started beside the first exits on the lock without writing. `--full` makes the run full. The collector then records the roster as the fleet in its ledger and prunes a cluster or project that left. It writes `reports/<finish-UTC>.md` and points `upgrade-retro-report.md` at it. A run without `--full` is scoped (section 7), whatever its `--project` set.

The collector writes two things. **The manifest**, at the path `--manifest-file` names, is the contract with `finish` (the fleet-audit collector-manifest design). **The report**, in its store on the shell volume, is the record and the source of every detail section 4 tailors.

**The manifest.** One entry per target under `clusters[]`, named `<project>/<location>/<name>`, each with an `outcome`. A `collected` entry carries `commands[]`: one `{check, command, rc}` per check of section 3 that ran there. It also carries `candidates[]`: one per incident the report lists under Errors or Warnings, with `check`, `namespace`, `object`, `severity`, `excerpt`, `command` and `impact`. An entry the collector could not read (`unreachable`, `gate-failed`) carries its `error` instead. Each entry also carries `facts`: the status and reasons, the versions before and after, the operations, the next upgrade and the guard ids written. A candidate's `impact` is the entry's row from the catalogue table, the generic (C). Three rules `finish` enforces from the manifest, so the document never states them by hand:

- A roster project whose `clusters list` failed does not demote the run. Each of its ledger-known clusters appears at `gate-failed`, with the listing error as its `error`. `cross_check_manifest` requires every one of them in `scope.skipped`, or in `scope.clusters` with `limitations`. `finish` then holds their carried findings rather than resolving them.
- A project whose `operations list` failed leaves `upgrade-operation-failed` out of every cluster's `commands`, and under `checks_unevaluated` on each reviewed cluster. `draft` writes the `limitations` note, and `finish` requires the check out of `checks_run`.
- A cluster whose upgrade is still in flight, and a cluster none of whose reads answered, are not `collected`. `draft` lists them in `scope.skipped` with the collector's error, and `finish` holds what the ledger carries for them.

With a repository, build the findings document from the manifest rather than by hand:

```bash
./skills/fleet-audit/scripts/audit_report.py draft --audit upgrade-retrospective --manifest-file /opt/data/scratch/manifest_upgrade-retrospective.json --out /opt/data/scratch/findings_upgrade-retrospective.json
```

`draft` writes every `collected` target to `scope.clusters`, with the commands at `rc == 0` as its `checks_run`. It writes every other enumerated target to `scope.skipped` with its error. It writes one finding per candidate, with its evidence, impact and severity. It leaves `recommendation` empty and `remediation` at `manual` for you. Sections 3 to 5 say what to verify and what to write. Never retype a `checks_run` entry. `finish` cross-checks each one against the manifest and refuses a check the collector did not run there.

**The report.** The store is `/opt/data/upgrade-retrospective/` on the shell sandbox's volume, where your terminal runs. Your file tools see the gateway's filesystem and do not reach it, so read it with the terminal. Under the root:

- `ledger.json`: the versions and the last symptom set per cluster.
- `guards.json`: one entry per classified failure and per risk shape, kept for the readiness watch to read once it ships. Nothing reads it yet.
- `reports/`: the newest fourteen full and fourteen scoped reports, Markdown and JSON by one stamp.
- `upgrade-retro-report.md`: a link to the newest full report.

After a full run, `readlink /opt/data/upgrade-retrospective/upgrade-retro-report.md` names `reports/<stamp>.md`, and `reports/<stamp>.json` is the detail. After a scoped run it is the newest `reports/*-scoped.json`. In that JSON:

- `sections.errors[]` and `sections.warnings[]` are the incidents the candidates stand for. Each carries `kind`, `severity`, `cluster`, `object`, `system`, `entries`, `predates_upgrade`, `what_happened`, `symptoms`, `mitigations` and `guards`; an operation incident carries `operation`.
- `sections.info` holds `clean` (every reviewed cluster's next upgrade, risks and baseline), `unchanged`, `upgrading`, `failed_reads`, `removed` and `rechecked`.
- `reviews[]` carries (A) under `what_happened` and (B) under `what_failed[]`.
- `scoped`, `scope_reason` and `outside_fleet[]` say what a scoped run covered.

The collector exits 2 with one line on stderr for a usage error or a crash record. A crash record is `ledger.json.unreadable-*` beside no ledger: a previous run's state that could not be read, set aside rather than overwritten. Do not run `--reset-ledger` yourself. It archives the record, and the next run starts from an empty ledger, which forgets every guard. Report the line as the run's outcome and stop. A `--dry-run` reads everything, prints the report and writes nothing; it is never a run of this stream.

### 3. Checks

**Run the collector (section 2) before grading anything by hand.** Every candidate in the manifest is one incident: one object on one cluster. The collector decided its check by the incident's kind, section and classifications, as the seven headings below say. Your part is to confirm each candidate against the report's incident, and to keep or lower its grade. A symptom the collector did not list is not an incident, and a shape in a cluster's risks table is never one.

**Finding identity.** **Do not write an `id`.** The harness derives it from `check`, `cluster`, `namespace` and `object`, and ignores any `id` in the file. `draft` fills the four from the candidate: `cluster` is the qualified `<project>/<location>/<name>`, `namespace` the object's namespace (`""` when it has none) and `object` the `Kind/name`. Never put a pod name, a version, a date or a count in `object`. The collector keys a symptom by the pod's owner, so a replaced pod is the same finding.

**Severity is a rule, not a judgement.** The collector grades an incident under `sections.errors` as a **`major`** candidate and one under `sections.warnings` as **`minor`**. Nothing this stream files is ever `critical`, and none of its check ids is on the fleet-audit skill's `MAJOR_SWEEP_CHECKS`. Together those two facts keep `finish` from opening a remediation pull request on this stream's own judgement. Keep the candidate's grade. You may lower a `major` to `minor` when the evidence says the classification is wrong, and say why in `impact`. Never raise one.

**`checks_run`.** `draft` copies each `collected` target's commands into `checks_run`, each entry an object in the shape the validator wants:

```json
{
  "check": "upgrade-broke-workload",
  "command": "KUBECONFIG=/opt/data/.kubeconfigs/kubeconfig_acme-prod_prod-usc1_us-central1-a.yaml kubectl get pods -A -o json"
}
```

`check` is the backticked slug from the heading below; `command` is the read as it ran. The validator rejects an unknown slug, a duplicate and a command under eight characters. It also rejects a command naming none of `kubectl`, `gcloud`, `gsutil`, `bq`, `helm` or `curl`, and a call back into `audit_report.py`. Leave the list as `draft` wrote it. A check added by hand fails the cross-check, and a check the collector left out did not run there.

#### 3.1 GKE reported an upgrade operation as failed (`upgrade-operation-failed`)

- **The collector emits it when:** an incident under `sections.errors` has `kind: operation`: an `UPGRADE_MASTER` or `UPGRADE_NODES` operation in the window ended with an error, or in a status other than `DONE`.
- **Not for:** an operation under `info.upgrading`, which has not ended.
- **Identity:** `namespace: ""`; `object` is `Cluster/<name>` for a control-plane target and `NodePool/<pool>` for a node pool.
- **Severity:** `major`.
- **Impact:** the operation, its target, when it ran and GKE's error text, from `operation`.
- **Remediation:** `kind: manual`. GKE's error text names the cause. The catalogue's entry 2 (no spare capacity) and entry 5 (window too short, exclusion mid-roll) are the usual ones.

#### 3.2 The upgrade broke a workload (`upgrade-broke-workload`)

- **The collector emits it when:** an incident under `sections.errors` has `kind: symptom` and its symptoms are not node conditions: a `high`-confidence catalogue match on an object outside the system namespaces, with onset inside the upgrade window.
- **Not for:** an incident whose `predates_upgrade` is true (it sits under `warnings`); a shape under `info.clean[].shapes`, which is a risk and never an incident.
- **Identity:** the incident's object (a budget is `PodDisruptionBudget/<name>` in its namespace).
- **Severity:** `major`.
- **Impact:** what failed and since when, from each symptom's `reason`, `since` and `evidence` (`n of m pods`, the message excerpt), with the catalogue entry number and title.
- **Remediation:** `kind: manual`; section 4 writes it from the entry's row.

#### 3.3 A node stayed broken after its pool's rebuild (`node-broken-after-upgrade`)

- **The collector emits it when:** an incident under `sections.errors` has a symptom of `category: node`: a node `NotReady` or `NetworkUnavailable` after an `UPGRADE_NODES` on its pool (entry 17, `high`).
- **Not for:** a node condition on a pool no operation in the window touched (the collector grades it `medium`, under `warnings`).
- **Identity:** `namespace: ""`, `object: Node/<name>`.
- **Severity:** `major`.
- **Impact:** the condition, its transition time and the pool's operation.
- **Remediation:** `kind: manual`, from entry 17's row: downgrade the pool while GKE still offers the previous version, and fix what the node agent selected on.

#### 3.4 A tentative match, or a sure one on the cluster's own plumbing (`upgrade-symptom-tentative`)

- **The collector emits it when:** an incident under `sections.warnings` has `kind: symptom`, `predates_upgrade` false, and at least one classification whose `entry` is not null: a `medium`-confidence match (a generic `OOMKilled`, a pull failure with no untouched node to compare, a Pending pod whose pool no operation touched), or a sure match on an object in a system namespace (`system: true`).
- **Not for:** an incident every one of whose classifications has `entry: null` (3.5).
- **Identity:** the incident's object.
- **Severity:** `minor`.
- **Impact:** the symptom, the entry it may match and why the match is tentative, from `evidence` and `detail`.
- **Remediation:** `kind: manual`; section 4, with the alternative entry named where the collector gives two (an `OOMKilled` single-container pod is "14 or 15" when the cgroup mode is unknown).

#### 3.5 A symptom matching none of the twenty (`upgrade-symptom-unclassified`)

- **The collector emits it when:** an incident under `sections.warnings` has `kind: symptom`, `predates_upgrade` false, and every classification `entry: null`.
- **Not for:** a healthy object. The collector lists only pods not Ready, pods Pending, nodes with a bad condition, and `Warning` events since the window's first operation.
- **Identity:** the incident's object.
- **Severity:** `minor`.
- **Impact:** the symptom as the collector read it, and that no catalogue entry matched. The report is a record of the upgrade, not only of the catalogue's part of it.
- **Remediation:** `kind: manual`. The entries the collector cannot identify from one read (3, 4, 5, 8, 9, 10, 11, 13, 16) are the ones the readiness checks have to catch before the upgrade. Say which, if any, the symptom resembles, and that a reader has to classify it.

#### 3.6 A symptom that predates the upgrade (`symptom-predates-upgrade`)

- **The collector emits it when:** an incident under `sections.warnings` has `predates_upgrade: true`: its onset is before the window's first operation, or the previous full run already recorded it, so the upgrade did not cause it.
- **Not for:** a first-run incident whose onset is inside the window. The first run has no stored set, grades by onset alone, and says so in `baseline`.
- **Identity:** the incident's object.
- **Severity:** `minor`.
- **Impact:** the symptom, its onset, and that it was present before the upgrade. It is reported so the next upgrade is not blamed for it either.
- **Remediation:** `kind: manual`. The entry's row still applies to the object, and the fix is the same, without the upgrade as its cause.

#### 3.7 A failure from an earlier review still present (`upgrade-failure-persists`)

- **The collector emits it when:** an incident has `kind: stale-guard`: a failure guard from an earlier run, on a cluster this run re-checked and still found it on, or could not re-check (`partial`, `event_only` or `recheck_errors` say which).
- **Not for:** a guard the re-check cleared (it is gone from `guards`); a risk guard, which is a shape and never an incident; a guard on a cluster the manifest did not mark `collected`, whose carried finding `finish` holds through `scope.skipped` and the still-flagged set.
- **Identity:** the guard's object, on the guard's `cluster`. Several stale guards on one object are one candidate and one finding; `title` and `impact` name every guard's entry.
- **Severity:** `minor`.
- **Impact:** the entry, when the guard was first seen and last seen, and why it is still live (still present, or not re-observable this run and why).
- **Remediation:** `kind: manual`: the entry's row, with what has not changed since the last report.

### 4. Tailor (C) and record (D)

The collector renders (C) from a table of the catalogue's twenty entries, in generic words. Your part is to say it about this object. For every finding `draft` wrote:

- **`title`**: name the entry and the object. `Entry 1 (a PodDisruptionBudget forbids the eviction) on seeded-upgrade/PodDisruptionBudget/pinned-batch-runner`, or `Unclassified: <reason> on <object>`, or `Operation UPGRADE_NODES on NodePool/<pool> ended <status>`.
- **`recommendation.action`**: the entry's `mitigate_after` (what repairs the cluster now) and `mitigate_before` (what to change before the next upgrade), rewritten for the object the finding names. Use its namespace and name, the field and value (`maxUnavailable: 0` and the replica count for entry 1, the selector key for entry 12, the image and tag for entries 14 and 20, the pool and its cgroup mode for 14, the driver and CUDA version for 18, the volume and the add-on for 19), taken from the candidate's `impact` and `excerpt`, and from the incident's `evidence`, `detail` and `mitigations` in the report JSON. Imperative, one or two sentences. An incident with no catalogue entry says what a reader should look at.
- **`recommendation.rationale`**: why this fix and not the obvious alternative, naming the alternative you rejected: deleting the budget instead of giving it room, raising the limit instead of moving to a runtime that reads cgroup v2, pinning the pool to cgroup v1 instead of fixing the image.
- **`recommendation.risk`**: what the change affects on apply, and the read-only command that confirms it first.
- **`remediation`**: `kind: manual`, always. The `note` names the object's manifest when the GitOps clone `start` handed you holds it (`grep -rl "name: <name>" --include='*.yaml' <workspace>`, then open each hit and confirm it declares this kind in this namespace before you name it; a hit is not a declaration until you have read it), the field and the value the change would set, and the guard ids the collector wrote for the incident (`facts.guards_written` in the manifest, `guards[].id` in the report), which stay in `guards.json` until a review finds the failure gone. Promise no report line for them: nothing reads that file yet. Never `kind: manifest` and never a file written into the workspace. The retrospective records what the upgrade did; the change it recommends is a user's decision, made by replying "apply" in chat, which goes through `submit-suggestion`. A `/remediate` comment naming one of these findings is answered by `finish` with a refusal, which is the intended shape.

(D)'s first half, the guards, the collector already wrote; name them. Its second half is the ledger issue, section 5.

### 5. The ledger issue, or the report alone

Where the install has a GitHub repository in `managed_repos`, this stream keeps what every fleet-audit stream keeps: one open ledger issue. Each run rewrites it in full, each finding is identified by cluster and object, and a run that finds nothing closes it. Where the install has no repository, the report on the volume is the record and this section is skipped. `start` and `finish` run **only on a full run**. A scoped run (section 7) never files, so a question about one clean cluster cannot close the fleet's issue.

`pending_remediation_requests` from `start` lists finding ids someone asked `/remediate` for. Every finding here is `manual`, so `finish` answers each request, and `/remediate all`, with one refusal naming the reason, and you write nothing for them. Every id in `carried` is in the document again when the manifest still carries its candidate. Otherwise list it under `resolved_because`, with the review that no longer shows it. A `carried` finding on a cluster in `scope.skipped` is neither, and `finish` holds it.

Before `finish`, self-check the document `draft` wrote and you completed:

- `audit` is `"upgrade-retrospective"`, and `scope.clusters` and `scope.skipped` are as `draft` built them.
- Every finding has its `title`, `impact`, `recommendation` (all three fields non-empty) and `remediation` (`kind: manual`, with a `note`).
- No two findings share a `(check, cluster, namespace, object)` quadruple, no `object` carries a pod name, version or date, and no finding names a skipped cluster.

A schema violation publishes nothing: `finish` exits 2 and the ledger is untouched. Validate here rather than discover it there.

```bash
./skills/fleet-audit/scripts/audit_report.py finish --audit upgrade-retrospective \
  --findings-file /opt/data/scratch/findings_upgrade-retrospective.json \
  --manifest-file /opt/data/scratch/manifest_upgrade-retrospective.json \
  [--repo "<owner>/<repo>"]
```

Pass `--manifest-file`. It lets `finish` hold the document to what the collector read. A `collected` target left out of `scope.clusters` is refused. A `checks_run` entry the manifest did not record at `rc == 0` is refused. Each finding's evidence is taken from its candidate. A previous finding the manifest still carries a candidate for is held, not announced as resolved. Pass `--no-collector-manifest "<why>"` only when the collector wrote no manifest at all; that publishes with the reason as a coverage gap, so the run is partial. One JSON line comes back with `status`, `issue_url`, `new`, `resolved`, `prs_opened`, `prs_closed`, `partial`, `coverage_gaps`, `silent_ok` and `unpublished_candidates`. Exit 2 means the validator rejected the document and nothing was published: fix the document and run `finish` again, never `start`. A `BROKER UNAVAILABLE` line means the document was fine and the broker went away, possibly after the ledger was rewritten: re-run `finish` once it answers. Exit 1 is fatal. Exit 0 means it published. `partial` is true when any cluster landed in `scope.skipped` or carries a `limitations` note. Then `resolved` is forced to `0` and no finding is announced as fixed. Even a findings-free run keeps the ledger open, with a comment naming the gaps. That is what an unread project or an upgrade still in flight should do. `prs_opened` is always `0` here, by section 3's severity rule and section 4's `manual`.

### 6. Report in chat

The run's final response is what `deliver: chat` posts. This stream's contract is one line per reviewed cluster: the counts and the most important finding. A quiet weekend, with no cluster new, upgraded or carrying a changed guard, posts nothing.

- **A scheduled run with nothing reviewed** ends in exactly `[SILENT]`, nothing else and no preamble, when section 5 was skipped or when `finish` returned `silent_ok: true`. Nothing reviewed means `reviews` empty, `info.rechecked` cleared and refreshed nothing, and `failed_reads` empty. A run that skipped a cluster, lost a project's listing or cleared a guard is never silent, however clean the rest: `silent_ok` is false for it, and the line names the gap or the guard that cleared.
- **A scheduled run that reviewed a cluster** always reports, whatever `silent_ok` says. A reviewed cluster is news even when it is clean, because the line tells the operator the upgrade was looked at. One line per reviewed cluster, in `sections` order: `<cluster>: <n> errors, <m> warnings; top: entry <k> (<title>) on <object>`, or `; clean, next upgrade <target> in window <next opens>`. Then one line naming the report (`/opt/data/upgrade-retrospective/upgrade-retro-report.md`, produced at `generated_at`) and the ledger `issue_url` in full where section 5 ran, or `no repository is linked: the report on the volume is the record` where it did not. Name every object and entry. A reader who never opens the file gets the verdict from these lines.
- **An on-demand run is never silent.** Someone is waiting on the answer; section 7 says what it carries.

### 7. On demand: the same report, from a question

This stream answers a question about what an upgrade did, or whether upgrades have problems. The question may use any words and name no feature, cluster or version. A question about what the _next_ upgrade would stop on is the `fleet-upgrade-verification` skill's readiness report instead; say so and route there.

1. **Find the latest saved report.** `readlink /opt/data/upgrade-retrospective/upgrade-retro-report.md` names the newest full report; its `.json` carries `generated_at`, `fleet` and `reviews`. No link means no full run has completed yet.
2. **Decide freshness against the clusters the question covers.** For each project those clusters are in (the roster of section 1 for a fleet-wide question; the named cluster's project otherwise), read the end time of the last terminal upgrade operation: `gcloud container operations list --project <p> --filter 'operationType:(UPGRADE_MASTER OR UPGRADE_NODES) AND status=DONE' --sort-by '~endTime' --limit 1 --format 'value(endTime,targetLink)'`. The report is fresh when `generated_at` is later than every such end time and every cluster the question covers is in its `reviews`, `unchanged` or `upgrading`.
3. **Fresh: answer from it.** Say when it was produced and name its path. Then, per cluster the question covers: whether the report reviewed it, its counts under Errors and Warnings, each incident's entry and object with the tailored (C) of section 4, and for a clean cluster the next upgrade and the risks present. Do not run the collector and do not call `start` or `finish`.
4. **Stale or missing: run first, then answer the same way.** A fleet-wide question is a full run: section 0's `start` where a repository is linked, then sections 1, 2 and 5 as on the scheduled route, then the answer, so the saved link and the ledger move with it. A question naming one or more clusters is a scoped run: `--cluster <project>/<location>/<name>` for each, with the cluster's own `--project`, **no `--full`** and no `--manifest-file`. It reviews those clusters even when unchanged, writes `reports/<stamp>-scoped.md` beside its JSON, moves no link, prunes nothing and files no ledger. Say in the answer that the fleet report and its issue were not touched. A cluster the last run did not review, named in the question, forces that cluster the same way.
5. **Mid-upgrade and locked.** A cluster under `upgrading` has an operation in flight: say it is upgrading now and is reviewed once the operation ends. The lock line of section 0 means a run is in progress: answer from the latest saved report with its age and say a fresh one is being written.

The answer names the report it came from, with its path, and is never `[SILENT]`. `silent_ok` from a full run's `finish` decides nothing about it, only whether the issue URL line reports a change.

## Red Lines

- **Never mutate.** No `gcloud container clusters upgrade`, `clusters update`, `node-pools update`, `operations cancel`, `kubectl apply/patch/delete/cordon/drain`, no `--reset-ledger`, and no change to a cluster's maintenance policy. Every fix is a recommendation a user turns into a pull request by asking.
- **Never open a pull request from this stream.** Every finding is `kind: manual`, an Error files at `major` and a Warning at `minor`, nothing at `critical`. A manifest written into the workspace or a `critical` severity is how `finish` would open one unasked.
- **Never grade from a read of your own.** The collector's classification, confidence and evidence are the finding's. A symptom the collector did not list is not an incident, and a shape in the risks table is never one.
- **Never write an unstable `object`.** The id is derived from `check`, `cluster`, `namespace` and `object`. A pod name, a version or a date in it reports the same failure as fixed and new again, on a ledger that exists to say what is still broken.
- **Never paste a credential into evidence.** No `masterAuth` block, client key, token, service-account key or kubeconfig contents in an `evidence.excerpt`. The collector's evidence strings are object names, reasons and message excerpts, and that is all an excerpt holds.
- **Never hand-write the ledger issue body, never comment on the ledger, and never touch git or the forge directly.** `audit_report.py` owns that surface; a scoped run and a run with no repository touch it not at all.
- **Never delegate to a Cluster Agent** or open kanban cards from this review; it is a self-contained fleet read.
