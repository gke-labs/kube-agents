# SOP: Upgrade Retrospective (Weekly Governance)

**Cron id:** `upgrade-retrospective` — `0 18 * * 0` (Sundays, 18:00 UTC), and once at onboarding, when the Chat Agent's first-run stage marks it due after the inventory scan settles.

**Purpose:** Say what the weekend's GKE upgrades _did_ to the workloads on every cluster the install manages, so the next cluster does not meet the same failure. The other upgrade streams look ahead; this one looks back. For every cluster that is new to it or was upgraded since its last run it records (A) which `UPGRADE_MASTER` and `UPGRADE_NODES` operations ran, from which version to which, (B) what stayed down, stuck or restarting afterwards and which of the upgrade failure catalogue's twenty entries each symptom matches, (C) how to see it coming and how to fix it, written for the cluster's own objects, and (D) the mitigation it set up: a guard the daily readiness report will carry until the failure is gone, and an entry in this stream's one ledger issue where a repository is linked. The report is **read-only**. It upgrades, rolls back, cordons, deletes and patches nothing, and it opens no pull request on its own.

**Data sources:** the collector `skills/fleet-audit/scripts/upgrade_retrospective.py` (which runs `gcloud container clusters list`, `gcloud container operations list`, `gcloud container get-server-config`, `gcloud container clusters get-credentials` and read-only `kubectl get`), `printenv GKE_PROJECT_ID`, the `list_cluster_profiles` tool for the roster, the collector's own store on the shell volume, and the GitOps clone `start` hands you for naming the file a fix would edit. **Nothing else.** No BigQuery, no Prometheus, no Cloud Logging, no external blueprint, no delegation to Cluster Agents via kanban. Every verdict is the collector's classification, with its evidence; your part is the grading in section 3, the tailoring in section 4, the ledger in section 5 and the chat line in section 6.

---

## Execution Checklist

### 0. Which run this is

Two routes reach this SOP, and the route decides the scope:

- **The scheduled tick and the first-run stage** are a **full run**: the whole roster (section 1), `--full` on the collector (section 2), the ledger issue where a repository is linked (section 5), one chat line per reviewed cluster (section 6).
- **A question someone asked** ("how did our upgrades go", "did anything break when the clusters were upgraded", "anything to fix before the next one", a question naming a cluster) is the on-demand route. Read section 7 first: it decides whether the saved report answers the question or a run is needed, and whether that run is full or scoped. Then apply the sections it names.

One run at a time. The collector holds an exclusive lock on `.lock` under its store for the whole run; a second run, scheduled or on demand, prints one line (`another retrospective run holds … since …; nothing written`) and exits 0. When you see that line, do not retry and do not wait it out: on the scheduled route report it as the run's one line and stop; on demand, answer from the latest saved report (section 7) and say a run is in progress.

### 1. The roster

The scheduled run reads the reconciler's roster, not every project the credential can list: the management project, and every project a Cluster Agent profile is pinned to.

1. `printenv GKE_PROJECT_ID` is the management project (SOUL.md §1; never hardcode it and never pass `-` as a project).
2. Call the `list_cluster_profiles` tool. It returns one entry per Cluster Agent profile with `project`, `cluster` and `location`; a profile scaffolded without its identity has only `name` and contributes no project.
3. The `--project` set is the management project plus every distinct `project` from step 2. A project named twice is passed once.

Without `--project` the collector reads the active project plus everything `gcloud projects list` returns, which is more than the install manages. Pass the set every time, on the scheduled route and on a fleet-wide on-demand run.

### 2. Run the collector

```bash
python3 ./skills/fleet-audit/scripts/upgrade_retrospective.py --full --project "$GKE_PROJECT_ID" --project <each further project from section 1>
```

Run it in the foreground, with a terminal timeout of at least 900 seconds, and never in the background: it reads every fleet cluster (one list call each for pods, nodes and the owners of both) even on a quiet week, re-reads every cluster that holds a guard, and a second run started beside the first exits on the lock without writing. `--full` is what makes the run full: it records the roster as the fleet in the collector's ledger, prunes a cluster or project that left, writes `reports/<finish-UTC>.md` and points `upgrade-retro-report.md` at it. A run without `--full`, whatever its `--project` set, is scoped (section 7) and never calls `start` or `finish`.

The store is `/opt/data/upgrade-retrospective/` on the shell sandbox's volume, where your terminal runs; your file tools see the gateway's filesystem and do not reach it, so read it with the terminal. Under the root: `ledger.json` (versions and the last symptom set per cluster), `guards.json` (one entry per classified failure and per risk shape, which the daily readiness report reads), `reports/` (the newest fourteen full and fourteen scoped reports, Markdown and JSON by one stamp), and `upgrade-retro-report.md`, a link to the newest full report. The run's Markdown report is also its stdout. Read the JSON beside it, not the Markdown: after a full run, `readlink /opt/data/upgrade-retrospective/upgrade-retro-report.md` names `reports/<stamp>.md`, and `reports/<stamp>.json` is the document every rule below reads; after a scoped run it is the newest `reports/*-scoped.json`.

The collector exits 2 with one line on stderr for a usage error or a crash record (`ledger.json.unreadable-*` beside no ledger: a previous run's state that could not be read, set aside rather than overwritten). Do not run `--reset-ledger` yourself: it archives the record and the next run starts from an empty ledger, which forgets every guard; report the line as the run's outcome and stop. A `--dry-run` reads everything, prints the report and writes nothing, and is never a run of this stream.

**What the JSON holds**, and the scope rules the findings document owes `finish` (section 5):

- `reviews[]` — one per cluster reviewed this run, keyed `cluster` as `<project>/<location>/<name>`, with `what_happened` (status `new`, `upgraded` or `forced`, the reasons, the versions before and after, the operations in the window with start, end, duration, status and error), `what_failed[]` (the symptoms, each with its `classifications[]` of catalogue `entry`, `confidence`, `evidence` and `detail`, its `since` and, where it predates the window, `predates_upgrade`), `mitigations[]` (the catalogue row per classified symptom: `title`, `before_signal`, `read_today`, `mitigate_before`, `mitigate_after`), `shapes[]` (the risk shapes present, never incidents), `guards[]` (what (D) wrote), `next_upgrade`, `baseline`, `read_errors[]`, `partial[]` (the core reads that failed), `answered[]`, and **`commands[]`**: the seven checks of section 3 whose read answered, each as `{check, command}` with the command that ran. Every reviewed cluster with a non-empty `commands` goes in `scope.clusters` as `{name: <its cluster key>, location, project, checks_run: <its commands, verbatim>}`; when `partial` is non-empty, add `limitations` naming the reads that failed and the checks absent from `commands`. A review whose `commands` is empty (its credentials or every core read failed) goes in `scope.skipped` with its `read_errors` as the reason.
- `unchanged[]` — every fleet cluster nothing happened to, each with `commands[]` too: the operations listing that found no upgrade backs every check, and the reads a re-check made for a live guard back the checks they decide. Each goes in `scope.clusters` with its `commands` as `checks_run`.
- `upgrading[]` — a cluster with an upgrade operation still in flight. Nothing was read from it: it goes in `scope.skipped` with the reason `upgrade in flight (<operation> <target> since <start>); reviewed on the next run`, and never in a finding.
- `failed_reads[]` — every read that failed, one string each. **On a full run, a roster project whose `clusters list` failed does not demote the run**: the collector keeps that project's ledger entries and guards unchanged and lists each of its ledger-known clusters here as `<project>/<location>/<name>: its project's listing failed (<error>); ledger entry and guards kept unchanged`. Copy every such entry into `scope.skipped` as `{cluster: <the key before the first colon>, reason: <the whole entry>}`. That is the harness's own mechanism for a cluster a run could not read: `finish` holds the stream's previous findings on those clusters rather than resolving them, keeps the ledger issue open, and announces nothing as fixed because a project went unread. The other entries (a `get-server-config` for a location, a re-check's read, a `--cluster` the project did not list) are limitations or skipped reasons on the cluster they name, as above.
- `removed_clusters[]` — clusters a successful listing no longer names (full run only): they left the fleet with their guards, go in neither scope list, and `finish` resolves whatever the ledger carried for them.
- `sections` — the three-way grouping the report renders: `errors[]` and `warnings[]` are the incidents section 3 grades, each `{kind, severity, cluster, object, system, entries, predates_upgrade, what_happened, symptoms, mitigations, guards}` and, for an operation incident, `operation`; `info` holds `clean` (every reviewed cluster's next upgrade, risks and baseline), `unchanged`, `upgrading`, `failed_reads`, `removed` and `rechecked`.
- `scoped` and `scope_reason` — true on any run without `--full`; `outside_fleet[]` names clusters a scoped run reviewed outside the recorded fleet, which are reported in chat and recorded nowhere.

A cluster appears in exactly one scope list, and nothing in `scope.skipped` may appear in a finding; the validator refuses the document otherwise.

### 3. Checks

**Run the collector (section 2) before grading anything by hand.** Every incident under `sections.errors` and `sections.warnings` is one object on one cluster, and every one becomes one finding; nothing under `info` does. The check is decided by the incident's `kind`, its section and its classifications, as the seven headings below say. Nothing here is graded from a read you make yourself: the collector read the cluster, and its `evidence` is the finding's.

**Finding identity.** **Do not write an `id`.** The harness derives it from `check`, `cluster`, `namespace` and `object`, and ignores any `id` in the file. `cluster` is the incident's `cluster` key, `<project>/<location>/<name>`, verbatim. The incident's `object` is `<namespace>/<Kind>/<name>` for a namespaced object and `<Kind>/<name>` for a cluster-scoped one: `namespace` takes the first segment (`""` when there is none) and `object` the `Kind/name` that remains, so one incident keeps one identity week over week. Never put a pod name, a version, a date or a count in `object`: the collector already keys a symptom by the pod's owner, so a replaced pod is the same finding.

**Severity is a rule, not a judgement.** An incident under `sections.errors` files at **`major`** and one under `sections.warnings` at **`minor`**. Nothing this stream files is ever `critical`, and none of its check ids is on the fleet-audit skill's `MAJOR_SWEEP_CHECKS`: together those two facts are what keeps `finish` from opening a remediation pull request on this stream's own judgement, which the design forbids. You may file an `errors` incident under a Warning's check at `minor` when its evidence says the classification is wrong (name why in `impact`); never the reverse, and never `critical`.

**`checks_run`.** Every cluster in `scope.clusters` carries the `commands` list the collector wrote for it, copied verbatim, each entry an object in the shape the validator wants:

```json
{
  "check": "upgrade-broke-workload",
  "command": "KUBECONFIG=/opt/data/.kubeconfigs/kubeconfig_acme-prod_prod-usc1_us-central1-a.yaml kubectl get pods -A -o json"
}
```

`check` is the backticked slug from the heading below; `command` is the read as it ran. The validator rejects an unknown slug, a duplicate, a command under eight characters, one naming none of `kubectl`, `gcloud`, `gsutil`, `bq`, `helm` or `curl`, and a call back into `audit_report.py`. A check the collector left out of a cluster's `commands` did not run there: name it in that cluster's `limitations` rather than adding it, and never round the list up to seven.

#### 3.1 GKE reported an upgrade operation as failed (`upgrade-operation-failed`)

- **Flag when:** an incident under `sections.errors` with `kind: operation`: an `UPGRADE_MASTER` or `UPGRADE_NODES` operation in the window ended with an error or in a status other than `DONE`.
- **Do NOT flag:** an operation under `info.upgrading`, which has not ended.
- **Identity:** `namespace: ""`; `object` is `Cluster/<name>` when `operation.target` is the control plane and `NodePool/<pool>` for a node pool.
- **Severity:** `major`.
- **Impact:** the operation, its target, when it ran and GKE's error text, from `operation`.
- **Remediation:** `kind: manual`. GKE's error text names the cause; the catalogue's entry 2 (no spare capacity) and entry 5 (window too short, exclusion mid-roll) are the usual ones, and `mitigations` on the same cluster's other incidents may already name the object.

#### 3.2 The upgrade broke a workload (`upgrade-broke-workload`)

- **Flag when:** an incident under `sections.errors` with `kind: symptom` whose symptoms are not node conditions: a `high`-confidence catalogue match on an object outside the system namespaces, with onset inside the upgrade window.
- **Do NOT flag:** an incident whose `predates_upgrade` is true (the collector puts it under `warnings`); a shape under `info.clean[].shapes`, which is a risk and never an incident.
- **Identity:** the incident's `object`, split as above (a budget is `PodDisruptionBudget/<name>` in its namespace).
- **Severity:** `major`.
- **Impact:** what failed and since when, from each symptom's `reason`, `since` and `evidence` (`n of m pods`, the message excerpt), with the catalogue entry number and title.
- **Remediation:** `kind: manual`; section 4 writes it from the entry's row.

#### 3.3 A node stayed broken after its pool's rebuild (`node-broken-after-upgrade`)

- **Flag when:** an incident under `sections.errors` whose symptom `category` is `node`: a node `NotReady` or `NetworkUnavailable` after an `UPGRADE_NODES` on its pool (entry 17, `high`).
- **Do NOT flag:** a node condition on a pool no operation in the window touched (the collector grades it `medium`, under `warnings`).
- **Identity:** `namespace: ""`, `object: Node/<name>`.
- **Severity:** `major`.
- **Impact:** the condition, its transition time and the pool's operation.
- **Remediation:** `kind: manual`, from entry 17's row: downgrade the pool while GKE still offers the previous version, and fix what the node agent selected on.

#### 3.4 A tentative match, or a sure one on the cluster's own plumbing (`upgrade-symptom-tentative`)

- **Flag when:** an incident under `sections.warnings` with `kind: symptom`, `predates_upgrade` false, and at least one classification whose `entry` is not null: a `medium`-confidence match (a generic `OOMKilled`, a pull failure with no untouched node to compare, a Pending pod whose pool no operation touched), or a sure match on an object in a system namespace (`system: true`).
- **Do NOT flag:** an incident every one of whose classifications has `entry: null` (3.5).
- **Identity:** the incident's `object`, split as above.
- **Severity:** `minor`.
- **Impact:** the symptom, the entry it may match and why the match is tentative, from `evidence` and `detail`.
- **Remediation:** `kind: manual`; section 4, with the alternative entry named where the collector gives two (an `OOMKilled` single-container pod is "14 or 15" when the cgroup mode is unknown).

#### 3.5 A symptom matching none of the twenty (`upgrade-symptom-unclassified`)

- **Flag when:** an incident under `sections.warnings` with `kind: symptom`, `predates_upgrade` false, and every classification `entry: null`.
- **Do NOT flag:** a healthy object; the collector lists only pods not Ready, pods Pending, nodes with a bad condition and `Warning` events since the window's first operation.
- **Identity:** the incident's `object`, split as above.
- **Severity:** `minor`.
- **Impact:** the symptom as the collector read it, and that no catalogue entry matched; the report is a record of the upgrade, not only of the catalogue's part of it.
- **Remediation:** `kind: manual`: the entries the collector cannot identify from one read (3, 4, 5, 8, 9, 10, 11, 13, 16) are the ones the readiness checks have to catch before the upgrade; say which, if any, the symptom resembles, and that a reader has to classify it.

#### 3.6 A symptom that predates the upgrade (`symptom-predates-upgrade`)

- **Flag when:** an incident under `sections.warnings` with `predates_upgrade: true`: its onset is before the window's first operation, or the previous full run already recorded it, so the upgrade did not cause it.
- **Do NOT flag:** a first-run incident whose onset is inside the window; the first run has no stored set and grades by onset alone, and says so in `baseline`.
- **Identity:** the incident's `object`, split as above.
- **Severity:** `minor`.
- **Impact:** the symptom, its onset, and that it was present before the upgrade; it is reported so the next upgrade is not blamed for it either.
- **Remediation:** `kind: manual`; the entry's row still applies to the object, and the fix is the same, without the upgrade as its cause.

#### 3.7 A failure from an earlier review still present (`upgrade-failure-persists`)

- **Flag when:** an incident with `kind: stale-guard`: a failure guard from an earlier run on a cluster this run re-checked and still found it on, or could not re-check (`partial`, `event_only` or `recheck_errors` say which).
- **Do NOT flag:** a guard the re-check cleared (it is gone from `guards`), or a risk guard, which is a shape and never an incident.
- **Identity:** the guard's `object`, split as above, on the guard's `cluster`.
- **Severity:** `minor`.
- **Impact:** the entry, when the guard was first seen and last seen, and why it is still live (still present, or not re-observable this run and why).
- **Remediation:** `kind: manual`: the entry's row, with what has not changed since the last report.

### 4. Tailor (C) and record (D)

The collector renders (C) from a table of the catalogue's twenty entries, in generic words. Your part is to say it about this object. For every finding:

- **`recommendation.action`**: the entry's `mitigate_after` (what repairs the cluster now) and `mitigate_before` (what to change before the next upgrade), rewritten for the object the finding names: its namespace and name, the field and value (`maxUnavailable: 0` and the replica count for entry 1, the selector key for entry 12, the image and tag for entries 14 and 20, the pool and its cgroup mode for 14, the driver and CUDA version for 18, the volume and the add-on for 19), taken from the incident's `evidence`, `detail` and `mitigations`. Imperative, one or two sentences. An incident with no catalogue entry says what a reader should look at.
- **`recommendation.rationale`**: why this fix and not the obvious alternative, naming the alternative you rejected: deleting the budget instead of giving it room, raising the limit instead of moving to a runtime that reads cgroup v2, pinning the pool to cgroup v1 instead of fixing the image.
- **`recommendation.risk`**: what the change affects on apply and the read-only command that confirms it first.
- **`remediation`**: `kind: manual`, always. The `note` names the object's manifest when the GitOps clone `start` handed you holds it (`grep -rl "name: <name>" --include='*.yaml' <workspace>`, then open each hit and confirm it declares this kind in this namespace before you name it; a hit is not a declaration until you have read it), the field and the value the change would set, and the guard ids the collector wrote for the incident (`guards[].id`), which the daily readiness report names until the failure is gone. Never `kind: manifest` and never a file written into the workspace: the retrospective records what the upgrade did, and the change it recommends is a user's decision, made by replying "apply" in chat, which goes through `submit-suggestion`. A `/remediate` comment naming one of these findings is answered by `finish` with a refusal, which is the intended shape.

(D)'s first half, the guards, the collector already wrote; name them. Its second half is the ledger issue, section 5.

### 5. The ledger issue, or the report alone

Where the install has a GitHub repository in `managed_repos`, this stream keeps what every fleet-audit stream keeps: exactly one open ledger issue, rewritten in full on every run, each finding identified by cluster and object, closed by a run that finds nothing. Where it has none, the report on the volume is the record and this section is skipped: the collector has already run, nothing here is required for the chat lines, and section 6 says so in them. Read the managed repository list the way `SOUL.md` §1 says (the `managed_repos` key of `$GITOPS_STATE_CONFIGMAP`); with no GitHub entry, go to section 6. `start` and `finish` are called **only on a full run**: a scoped run (section 7) never files, so a question about one clean cluster cannot close the fleet's issue.

```bash
./skills/fleet-audit/scripts/audit_report.py start --audit upgrade-retrospective [--repo "<owner>/<repo>"]
```

If multiple repositories are registered in `managed_repos`, pass `--repo "<owner>/<repo>"` explicitly: on a scheduled run iterate over all of them in sequence, running `start` and `finish` for each; on an on-demand full run do the same unless the question named one. Returns `{"issue": <int|null>, "repo": "org/repo", "workspace": "...", "findings_path": "/opt/data/scratch/findings_upgrade-retrospective.json", "carried": [...], "pending_remediation_requests": [...], ...}`. Keep `findings_path` (you write the document there), `workspace` (the clone section 4 greps for a manifest's path) and `carried` (the finding ids the previous run published). A `START REFUSED` line means a run of this stream is in flight, a scheduled tick's or another session's: stop and say so; there is no override. A `BROKER UNAVAILABLE` line is the credential proxy not answering: re-run the same command once it does. `start` creates and resets no branch; the helper owns all git and forge work and renders the ledger body itself. **Never hand-write a ledger body, never run `git commit` or a `vcs.py` verb yourself, and never comment on the ledger.** `pending_remediation_requests` lists finding ids someone asked `/remediate` for; every finding here is `manual`, so `finish` answers each request, and `/remediate all`, with one refusal naming the reason, and you write nothing for them.

Write the document to `findings_path` with `audit: "upgrade-retrospective"`, `scope.clusters` and `scope.skipped` exactly as section 2's rules build them from the JSON, and `findings`, `[]` for a run whose two incident sections are empty. Every finding carries `check`, `severity`, `title`, `cluster`, `namespace`, `object`, `impact`, `evidence` (`command`: the cluster's `commands` entry for this check; `excerpt`: the classification's `evidence` text, trimmed to the lines that prove it), `recommendation` with all three fields, and `remediation` with `kind: manual` and its `note`. `title` names the entry and the object: `Entry 1 (a PodDisruptionBudget forbids the eviction) on seeded-upgrade/PodDisruptionBudget/pinned-batch-runner`, or `Unclassified: <reason> on <object>`, or `Operation UPGRADE_NODES on NodePool/<pool> ended <status>`. Every id in `carried` is either filed again, when the JSON still shows it, or listed under `resolved_because` with the review that no longer shows it; a `carried` finding on a cluster in `scope.skipped` is neither, and `finish` holds it. Before writing, self-check: no two findings sharing a `(check, cluster, namespace, object)` quadruple, no `object` carrying a pod name, version or date, every `evidence.command` a command from the collector's `commands`, every `scope.clusters` entry carrying a non-empty `checks_run` or a `limitations` saying why nothing ran, the two scope lists disjoint, and no finding on a skipped cluster. A schema violation publishes nothing, `finish` exits 2 and the ledger is untouched; validate here rather than discover it there.

```bash
./skills/fleet-audit/scripts/audit_report.py finish --audit upgrade-retrospective \
  --findings-file /opt/data/scratch/findings_upgrade-retrospective.json \
  [--repo "<owner>/<repo>"]
```

Pass neither `--manifest-file` nor `--no-collector-manifest`: the collector writes this stream's report, not a collector manifest in the shape `finish` cross-checks, and `finish` asks this stream for neither. One JSON line comes back with `status`, `issue_url`, `new`, `resolved`, `prs_opened`, `prs_closed`, `partial`, `coverage_gaps` and `silent_ok`. Exit 2 means the validator rejected the document and nothing was published: fix the document and run `finish` again, never `start`. A `BROKER UNAVAILABLE` line means the document was fine and the broker went away, possibly after the ledger was rewritten: re-run `finish` once it answers. Exit 1 is fatal. Exit 0 means it published. `partial` is true when any cluster landed in `scope.skipped` or carries a `limitations` note; then `resolved` is forced to `0`, no finding is announced as fixed, and even a findings-free run keeps the ledger open with a comment naming the gaps, which is exactly what an unread project or an upgrade still in flight should do. `prs_opened` is always `0` here, by section 3's severity rule and section 4's `manual`.

### 6. Report in chat

The run's final response is what `deliver: chat` posts, and this stream's contract is one line per reviewed cluster: the counts and the most important finding. A quiet weekend, with no cluster new, upgraded or carrying a changed guard, posts nothing.

- **A scheduled run with nothing reviewed** — `reviews` empty, `info.rechecked` cleared and refreshed nothing, `failed_reads` empty — ends in exactly `[SILENT]`, nothing else and no preamble, when section 5 was skipped or when `finish` returned `silent_ok: true`. A run that skipped a cluster, lost a project's listing or cleared a guard is never silent, however clean the rest: `silent_ok` is false for it, and the line names the gap or the guard that cleared.
- **A scheduled run that reviewed a cluster** always reports, whatever `silent_ok` says: a reviewed cluster is news even when it is clean, because the line is what tells the operator the upgrade was looked at. One line per reviewed cluster, in `sections` order: `<cluster>: <n> errors, <m> warnings; top: entry <k> (<title>) on <object>` or `; clean, next upgrade <target> in window <next opens>`, then one line naming the report (`/opt/data/upgrade-retrospective/upgrade-retro-report.md`, produced at `generated_at`) and the ledger `issue_url` in full where section 5 ran, or `no repository is linked: the report on the volume is the record` where it did not. Name every object and entry; a reader who never opens the file gets the verdict from these lines.
- **An on-demand run is never silent.** Someone is waiting on the answer; section 7 says what it carries.

### 7. On demand: the same report, from a question

A question about what an upgrade did, or whether upgrades have problems, in any words and naming no feature, cluster or version, is answered by this stream. A question about what the _next_ upgrade would stop on is the `fleet-upgrade-verification` skill's readiness report instead; say so and route there.

1. **Find the latest saved report.** `readlink /opt/data/upgrade-retrospective/upgrade-retro-report.md` names the newest full report; its `.json` carries `generated_at`, `fleet` and `reviews`. No link means no full run has completed yet.
2. **Decide freshness against the clusters the question covers.** For each project those clusters are in (the roster of section 1 for a fleet-wide question; the named cluster's project otherwise), read the end time of the last terminal upgrade operation: `gcloud container operations list --project <p> --filter 'operationType:(UPGRADE_MASTER OR UPGRADE_NODES) AND status=DONE' --sort-by '~endTime' --limit 1 --format 'value(endTime,targetLink)'`. The report is fresh when `generated_at` is later than every such end time and every cluster the question covers is in its `reviews`, `unchanged` or `upgrading`.
3. **Fresh: answer from it.** Say when it was produced and name its path, then per cluster the question covers: whether the report reviewed it, its counts under Errors and Warnings, each incident's entry and object with the tailored (C) of section 4, and for a clean cluster the next upgrade and the risks present. Do not run the collector and do not call `start` or `finish`.
4. **Stale or missing: run first, then answer the same way.** A fleet-wide question is a full run: sections 1, 2 and 5 as on the scheduled route, then the answer, so the saved link and the ledger move with it. A question naming one or more clusters is a scoped run, `--cluster <project>/<location>/<name>` for each with the cluster's own `--project` and **no `--full`**, which reviews those clusters even when unchanged, writes `reports/<stamp>-scoped.md` beside its JSON, moves no link, prunes nothing and files no ledger: say in the answer that the fleet report and its issue were not touched. A cluster the last run did not review, named in the question, forces that cluster the same way.
5. **Mid-upgrade and locked.** A cluster under `upgrading` has an operation in flight: say it is upgrading now and is reviewed once the operation ends. The lock line of section 0 means a run is in progress: answer from the latest saved report with its age and say a fresh one is being written.

The answer names the report it came from, with its path, and is never `[SILENT]`; `silent_ok` from a full run's `finish` decides nothing about it, only whether the issue URL line reports a change.

## Red Lines

- **Never mutate.** No `gcloud container clusters upgrade`, `clusters update`, `node-pools update`, `operations cancel`, `kubectl apply/patch/delete/cordon/drain`, no `--reset-ledger`, and no change to a cluster's maintenance policy. Every fix is a recommendation a user turns into a pull request by asking.
- **Never open a pull request from this stream.** Every finding is `kind: manual`, an Error files at `major` and a Warning at `minor`, nothing at `critical`; a manifest written into the workspace or a `critical` severity is how `finish` would open one unasked.
- **Never grade from a read of your own.** The collector's classification, confidence and evidence are the finding's; a symptom the collector did not list is not an incident, and a shape in the risks table is never one.
- **Never write an unstable `object`.** The id is derived from `check`, `cluster`, `namespace` and `object`; a pod name, a version or a date in it reports the same failure as fixed and new again on a ledger that exists to say what is still broken.
- **Never paste a credential into evidence.** No `masterAuth` block, client key, token, service-account key or kubeconfig contents in an `evidence.excerpt`; the collector's evidence strings are object names, reasons and message excerpts, and that is all an excerpt holds.
- **Never hand-write the ledger issue body, never comment on the ledger, and never touch git or the forge directly.** `audit_report.py` owns that surface; a scoped run and a run with no repository touch it not at all.
- **Never delegate to a Cluster Agent** or open kanban cards from this review; it is a self-contained fleet read.
