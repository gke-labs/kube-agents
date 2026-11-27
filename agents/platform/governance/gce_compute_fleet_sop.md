# SOP: GCE Compute Engine and MIG Fleet Audit (Daily Governance)

**Purpose:** Sweep all managed GCE Compute Engine instances and Managed Instance Groups (MIGs) across target GCP projects for failed startup scripts, MIGs that cannot converge on their target size, sole-tenant headroom exhaustion, orphaned storage snapshots, and GKE nodes whose kernel dropped connections on a full connection tracking table. The question this audit answers for a platform admin is: _which standalone VMs or MIG instances have failed startup scripts, which MIGs are stuck in a resize loop, which storage snapshots belong to deleted disks, and which GKE node pools dropped connections on a full connection tracking table?_ Output is this stream's single GitHub ledger issue, rewritten in place on every run, plus narrow remediation Pull Requests carrying Terraform or manifest fixes for the findings that get promoted.

**Cron:** id `gce-compute-fleet-audit`, schedule `45 7 * * *` (daily 07:45 UTC).

**Data sources:** `gcloud compute instances ...`, `gcloud compute instance-groups ...`, `gcloud compute resource-policies ...`, `gcloud compute snapshots ...`, and `gcloud logging read ...` over the serial console log, run once per project in the resolved project scope (§1).

---

## Execution Checklist

### 0. Open the audit run

```bash
python3 /opt/data/profiles/platform/skills/fleet-audit/scripts/audit_report.py start --audit gce-compute-fleet-audit
```

Returns `{"issue": <int|null>, "repo":"org/repo", "workspace":"/opt/data/gitops/gce-compute-fleet-audit/org__repo", "findings_path":"/opt/data/scratch/findings_gce-compute-fleet-audit.json", "pending_remediation_requests": [<finding_id>, ...], "declared_intent_repos":[…], "declared_intent_searched":[…], "declared_intent_unsearched":[…], "declarations_path":"…"}`. Keep `findings_path`, `workspace`, `declared_intent_repos`, `declared_intent_searched` and `declared_intent_unsearched` from this call: you write into the first two and read the rest in §2a — `declared_intent_repos` is the set the search must cover, `declared_intent_searched` what `start` already read, `declared_intent_unsearched` the repositories (with their pinned `ref`) that are yours to copy and record; `declarations_path` is where `start` filed the notes it found. The obtainability SOP's Step 0 says what each holds in full.

If `pending_remediation_requests` is non-empty, inspect each requested finding in the open issue and write the updated manifest or Terraform file to `workspace` at `remediation.path` before proceeding to step 3 (`finish`).

### 1. Enumerate the target fleet

**Resolve the project scope first.** Call the platform_control `fleet_scope` tool first. When it reports `declared: true`, the scope is its `projects`, every project the install's declared scope resolved to and could read, and nothing else: pass its `collector_args` to the collector verbatim (`--scope-projects …`, with `--scope-unread …` naming the declared projects the install could not read, which the run records as a coverage gap), and run any manual per-project command over that list alone, never over `gcloud projects list`. When no declared project is readable, `collector_args` carries `--scope-unread` alone: pass it anyway, and the collector reports that nothing was readable and exits with a top-level `error`, which §2 handles. An answer that starts with `ERROR:` means the scope is unknown: list nothing and report the error as your one-line summary. When it reports `declared: false` the install declares no scope, and the scope is the host project (`gcloud config get-value project`) plus every project `gcloud projects list --format="value(projectId)"` returns. Run every collection command once per project, passing `--project` explicitly — the ambient default silently audits one project and reports the result as a fleet sweep. On an install with no declared scope the scope is what the agent's identity can read, so an operator narrows it by narrowing the IAM grant; on one with a declared scope, `spec.scope` is the boundary, and the IAM grant follows it. A listing that exits non-zero, or that returns without the host project, cannot say how many other projects exist: sweep the projects you have and add one `scope.skipped` entry, `{"cluster": "project/UNENUMERATED_PROJECTS", "reason": "<the listing's rc and stderr excerpt, or the host project it omitted>"}`, so the run publishes as partial rather than as the whole fleet. A run narrowed on request — someone asks for one project, or a helper script is given `--project-id` or `MONITORED_PROJECT_IDS` — records the same entry with the reason `scope narrowed to <projects> on request`: it read no other project, and without the entry `finish` resolves every ledger finding on a project the run never looked at. A project where the Compute Engine API is disabled (`SERVICE_DISABLED`, `accessNotConfigured`, `has not been used in project`) holds nothing to audit and counts as empty, not skipped: recording it as a loss would pin every run partial for as long as the project exists. That holds only when the refusal names this project — its id, or the number `gcloud projects describe <project> --format='value(projectNumber)'` prints. A refusal naming another project, such as the credential's quota project, says nothing about this one: record it in `scope.skipped` as a failed read. The collector (§2) applies all of this itself: it leaves out a project whose own API is off; one holding no instance, MIG, node group or snapshot is listed with all five checks in `checks_not_applicable`, which `finish` takes as fully accounted for; and hands back `project/UNENUMERATED_PROJECTS` as a `gate-failed` target carrying the reason.

- **Pass `--project` explicitly on every collection command.** Never rely on the ambient default: it silently audits one project and reports the result as a fleet sweep.
- Record each resolved project as `{name: "project/" + project_id, location: "global", project: project_id, checks_run: [...]}` into `scope.clusters`. The `project/` prefix is what makes the scope line say "project"; a hyphen there is read as a bare cluster name.
- **`checks_run` is mandatory on every scope entry:** Each entry is an object `{"check": "<slug>", "command": "<literal command>"}` naming the exact inspection command executed on that project target.
- **This audit sees the Standard-mode fleet only.** The Compute Engine API does not return a GKE Autopilot node's `gk3-*` instance, its managed instance group, or its boot disk to the Platform Agent identity — a 404 rather than a 403, [by design](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/autopilot-architecture) rather than for want of a grant. That is the correct universe: Google manages those nodes, so §2.1's startup scripts and §2.2's convergence are not the operator's to set or to fix, and a finding on one would carry a recommendation nobody can act on. Do not record it as a coverage gap or a `limitations` note — this stream's only target is the project, so a gap on it holds every GCE finding open forever. The case that does need declaring is a project whose nodes are all Autopilot: it enumerates empty, and the collector declares §2.1, §2.2 and §2.6 in `checks_not_applicable` rather than letting an unseen fleet read as clean.
- A project or target you cannot reach goes in `scope.skipped` as `{"cluster": "project/" + project_id, "reason": "<why>"}`. The key is `cluster`, not `name` — a skipped entry keyed the way the `scope.clusters` entry above is keyed fails validation and `finish` publishes nothing, which discards the whole run at exactly the moment part of the fleet was unreadable. The sweep continues — one project's permission error never decides the outcome for the rest of the fleet. If a target is partially readable, record the refusal in its `limitations` string. Declare structurally inapplicable checks in `checks_not_applicable`.

### 2. Diagnostic checks roster

**Run the collector before evaluating any check below by hand.**

```bash
python3 /opt/data/profiles/platform/skills/gce-compute-fleet-audit/scripts/compute_fleet_audit.py <collector_args from the fleet_scope tool, verbatim, or nothing on an install that declares no scope> > /opt/data/scratch/manifest_gce-compute-fleet-audit.json
```

**Then run the precheck on the manifest, before reading it or evaluating any check by hand.**

```bash
python3 /opt/data/profiles/platform/skills/fleet-audit/scripts/audit_report.py precheck --audit gce-compute-fleet-audit --manifest-file /opt/data/scratch/manifest_gce-compute-fleet-audit.json
```

The cron prompt of a scheduled run names the flag to add to this command, the same one it names for `finish`; on a run a person asked for, add it to neither. It prints one JSON line. A line carrying `precheck: unchanged` is `finish`'s own line: the collector found the fleet exactly as the last scheduled run left it, so the run has been closed from that run's stored findings document, and nothing in the rest of this SOP is left for you to do — read `silent_ok` from that line and report as the closing step of this SOP says (exactly `[SILENT]` when it is `true`). Any other line (`verdict` with `closed: false`) means nothing was closed: continue this SOP from here as on any run. A `BROKER UNAVAILABLE` line means the credential proxy did not answer: wait for it and run the same `precheck` again. An exit 1 (`FATAL`) means the close died partway, with your findings file put back as it was: run the same `precheck` once more, and if it fails again continue this SOP from here.

This stream's targets are GCP projects, not GKE clusters, so its collector is its own script rather than `fleet-audit`'s `collect.py` — see the script's own module docstring for the field contracts it assumes of each `gcloud` command's JSON. Pass `--project-id <id>` only to scope a run to one project the tool lists, with the `collector_args` beside it. Left alone it resolves the scope §1 describes — the `--scope-projects` you pass from `fleet_scope`, else `MONITORED_PROJECT_IDS` when set, which narrows the run on purpose, and otherwise the configured project plus every project `gcloud projects list` returns — so without a declared scope an identity that can list the organisation sweeps every project it can see rather than the one it is configured for. On this install's sandbox the collector knows whether a scope is declared and, if it is, refuses a run without the `collector_args`, so the fall-through cannot cross the boundary. Read the manifest before doing anything else:

- Every entry in `manifest.clusters` is one project target, named `project/<project_id>`, carrying one `outcome`. `"collected"` means every check the collector implements already ran; do not re-run it by hand. `"gate-failed"` means a project-level read the whole target depends on failed (an instance listing that did not complete is the usual one): put it in `scope.skipped` with its `error` as the reason. A failed read of one instance's console is not that, and stays on a `"collected"` target as a `limitations` note. `project/UNENUMERATED_PROJECTS` is always `gate-failed`: it stands for the projects the run did not enumerate, so it goes in `scope.skipped` the same way. A manifest with a top-level `error` read no project at all: do not call `finish`, and report the error as your one-line summary. The collector exits non-zero for it.
- For a `"collected"` target, copy its `commands` list into that target's `checks_run` — minus any entry whose `check` that same target also lists in `checks_not_applicable` or `checks_unevaluated`. A `commands` entry records that a command ran, not that the check reached a verdict on that target, so one read is routinely recorded against slugs it could not answer for; `finish` rejects a `checks_run` naming a slug the collector declared inapplicable there. Copy that target's `checks_not_applicable` and its `limitations` string verbatim too.
- **The roster is five checks and the collector implements all five**, so there is nothing below for you to hand-run. §2.3 is a numbered slot rather than a check: `ops-agent-guest-health` is not on the roster and `finish` rejects a `checks_run` or a `finding.check` naming it. Where a target does carry a `checks_not_applicable` entry, the list you publish is the collector's, unchanged: copy it, and where a target has none, publish none.
- **`checks_not_applicable` and `checks_unevaluated` mean different things**, and neither is yours to rewrite. A check in `checks_not_applicable` could not apply — the project reserves no sole-tenant node groups, so §2.4 has no reservation to measure. A check in `checks_unevaluated` applied but reached no verdict: the collector read the surface and no figure came back, or every read the check depends on failed (every qualifying console refused, say). Put it in neither `checks_run` nor `checks_not_applicable`; the target's `limitations`, which you copy, names it, and that makes the run partial so its findings are not called resolved off this run. `finish` refuses a document that files it either way.
- Every entry in a `"collected"` target's `candidates` is a verified finding: `check`, `object`, `severity`, `impact` and `excerpt` are already computed, and `finish` overwrites your `evidence` with the collector's. What is still yours to write is the `title` and the `recommendation`, and for a `kind: manifest` remediation the Terraform or manifest file itself (§3).
- **A candidate carrying `needs_triage` is the one place your judgment still decides whether it ships.** The collector applies the mechanical condition and not the check's _Do NOT flag_ clause, so it names the exclusion it could not apply. There are four, and what you do with one depends on whether you can settle it. `gke-managed-node` (§2.1) means confirm the instance is not a GKE node pool member, and `gke-managed-mig` (§2.2) means decide whether the churn is pod-driven; both are readable, so read them and drop the candidate where the exclusion holds. `retention-hold` (§2.5) and `maintenance-window` (§2.4) name surfaces this audit cannot reach at all. **An exclusion you cannot read is not an exclusion you have established: publish, and put the check the reader still owes into the recommendation.** Dropping on a suspicion is how a stream comes to report a clean fleet over findings it never disproved. A `retention-hold` candidate a filed declaration covers (§2a) is still written to `findings`: `finish` moves it, and the note is the answer to the check the reader owed. `needs_triage` is not a findings-schema field — do not copy it into the document.
- Pass `--manifest-file <path>` to `finish` (§5) so it cross-checks your `checks_run` against what the collector actually ran.

#### 2.1 Instance startup script failures in serial port output (`gce-startup-script-status`)

- **Severity**: `critical`
- **Command**: `gcloud compute instances get-serial-port-output $VM --zone=$ZONE --project=$PROJECT --port=1`
- **Condition**: VM serial port console output contains a fatal startup script error: `Script "startup-script" failed with error` (the current guest agent), or `startup-script exit status <non-zero>` or `Finished running startup scripts with error` (older agents).
- **Do NOT flag**: GKE node pool instances managed directly by GKE control plane or instances cleanly completing boot without errors.
- **Remediation**: Correct boot metadata or deployment configuration in instance template or Terraform definition.

The collector reads only the consoles of instances that run a startup script at all — its own metadata sets `startup-script` or `startup-script-url`, or the project's common metadata does and therefore every instance's does. Where no instance qualifies it declares the check in `checks_not_applicable` rather than reporting it clean. All three markers above are printed by `google_metadata_script_runner`, which does not run when no script is set, so a console with no script behind it cannot carry any of them however badly the instance booted; reading such consoles and finding nothing is a pass that was never capable of failing. A fleet of GKE Standard nodes is exactly that shape: they bootstrap from `user-data` and `kube-env` and set neither startup-script key. Read "can see" strictly: an Autopilot node does set `startup-script`, to a Google-installed stub that neutralises the hook, so the generalisation holds for the Standard fleet rather than for GKE nodes as a class. It changes nothing about what the check should do — the scope note above already puts those nodes outside this audit's universe, and they never reach the count the `checks_not_applicable` reason quotes. A `compute project-info describe` that fails leaves every RUNNING instance a target, because skipping the check across a whole project on the strength of a read that did not happen is the worse error.

The serial port output is a ring buffer of about 1 MiB, and the guest prints its startup-script lines once per boot. A VM that logs often pushes them out within weeks, so on an instance known to run a script, a console with no failure line is a clean result only when it still shows the start of the last startup-script run (`Running metadata script runner (version …) for "startup" event`, `Starting startup scripts`, or systemd's `Starting google-startup-scripts.service` when runner output follows it; without runner output after it, that run has not started yet, and the previous run counts). When the start is gone, the check still ran: the collector does not list it in `checks_unevaluated` or in `limitations`, because a VM that logs often reaches this state within weeks, and a coverage gap would hold every GCE finding open. Instead the target's `notes` names those instances (GKE nodes excluded; at most ten names, then ", and N more"), and its `noted_objects` lists every one of them. `finish` copies the note from the manifest and does not count it as a gap. It puts the note under `notes` on its JSON line on every status, with ready-made `report_notes` lines beside it on the two forge statuses (`NO_FORGE` and `STORE_LEDGER` carry the notes inside `summary`, which you post verbatim); in the Notes column of the ledger's scope table and in the body's own "Read but not visible" list, which carries the full note, when it writes the body; and in the clean or held comment when it posts one. Do not report those instances as clean, and do not file new findings on them: their startup-script status is not visible. Copy each line of `report_notes` into your report verbatim, directly before the closing ledger line — the ledger URL stays the last line — except on a run whose `silent_ok` is `true`: that run answers `[SILENT]` and does not relay the notes (`/opt/data/profiles/platform/skills/fleet-audit/references/finish.md`). `finish` makes sure the channel hears each change once: a scheduled run is not silent when its set of noted instances is different from the set of the last scheduled run — a new noted instance, or one whose console shows its startup lines again — and its line then carries `noted_changed: true`. When the set is the same, the notes alone do not stop `silent_ok`, so a long-lived noted instance does not wake the channel each day. A previous finding on one of those instances (`start` lists it under `carried`) did not go away in this run's sight: carry it forward, which means report it again in `findings` from the previous finding. Do not write it in `resolved_because`; `finish` refuses that entry. If you leave it out, `finish` holds it on the ledger and does not count it as resolved. A noted instance with no previous finding holds nothing.

#### 2.2 Managed Instance Group convergence stalled (`mig-convergence-stalled`)

- **Severity**: `major`
- **Command**: `gcloud compute instance-groups managed list --project=$PROJECT --format=json`
- **Condition**: A group's `currentActions` shows `creating` and `deleting` both non-zero: it is adding and removing instances at the same moment, which is a resize loop and not a scale event.
- **Do NOT flag**: GKE node pool groups (`gke-` or `gk3-` prefix) undergoing standard pod-driven scale events. The collector cannot tell a pod-driven churn from a pathological one, so it hands these back carrying `needs_triage: gke-managed-mig`. A group with an update in progress (`status.versionTarget.isReached: false`), because a rolling update with surge or a GKE surge upgrade creates and deletes at once by design; the collector skips these itself. Also do not flag `status.isStable: false` on its own: instability is the normal state of any group mid-scale.
- **Remediation**: Adjust the autoscaling cool-down period and utilization targets in the MIG specification.

This slug replaced `mig-autoscaler-flapping`, whose condition was a rate — repeated resizes inside fifteen minutes. No `gcloud` read carries a MIG's resize history to count one, so the rate was never measurable; a resize loop caught in the act is the part of that intent a single point-in-time read can establish, and the slug now says what it measures. A group whose no-retry creation failed is not measurable either: `creatingWithoutRetries` counts creations still to be tried, and a failed one lowers `targetSize`, so the group reads as converged on its smaller target. On a ledger that already carries a `mig-autoscaler-flapping` or `ops-agent-guest-health` finding, the first run after this roster change reports it resolved: neither slug is checked any more, so nothing re-examines it.

#### 2.3 Compute Engine Ops Agent guest telemetry (not audited by this stream)

There is no `ops-agent-guest-health` slug on this stream's roster, and `finish` rejects a `checks_run` or a `finding.check` naming one. Do not hand-run this check and do not publish a `checks_not_applicable` entry for it either.

It is recorded here because the gap is deliberate rather than forgotten. Whether a guest's Ops Agent is reporting lives in Cloud Monitoring or in OS Config inventory, and neither surface is reachable: `gcloud monitoring` exposes no metric read the credential proxy's allowlist could carry, and `osconfig.googleapis.com` is not enabled on the reference install. Carrying the slug as permanently unevaluated was worse than dropping it: it would make every run partial, so one unimplementable check would keep every finding on the other checks from ever being announced resolved and their remediation pull requests from ever closing. If the surface becomes reachable, this section is where the check goes back.

#### 2.4 Sole-tenant node group reservation headroom exhaustion (`sole-tenant-headroom`)

- **Severity**: `minor`
- **Command**: `gcloud compute sole-tenancy node-groups list --project=$PROJECT --format=json`, then `gcloud compute sole-tenancy node-groups list-nodes $GROUP --zone=$ZONE --project=$PROJECT --format=json` per group.
- **Condition**: Consumed vCPU or memory reaches 90% of the group's aggregate capacity **and** less than one node's worth of vCPU is still free. Both halves are required: a group at 90% across ten nodes still has a whole node spare and survives losing one, which is what "without failover host headroom" means.
- **Do NOT flag**: Node groups with autoscaling enabled — the collector applies this one itself, because `autoscalingPolicy.mode` is a field on the group rather than a judgment about it. Planned maintenance windows are not readable, so candidates come back carrying `needs_triage: maintenance-window`.
- **Remediation**: Add capacity or expand the sole-tenant node group reservation.

A project reserving no sole-tenant node groups gets a `checks_not_applicable` entry: the enumeration ran and came back empty, which is a structural absence the read positively established, not a measurement that failed. A group holding no nodes is measured too: it has no host to lose. `checks_unevaluated` is reserved for the case where the groups exist and, for every one of them, `list-nodes` failed or no node carried the `totalResources`/`consumedResources` figures the ratio needs.

#### 2.5 Orphaned Persistent Disk snapshots from deleted source disks (`orphaned-snapshots`)

- **Severity**: `minor`
- **Command**: `gcloud compute snapshots list --project=$PROJECT --format=json`
- **Condition**: Snapshot older than 90 days whose `sourceDisk` matches no live disk in the project, and that no snapshot schedule took.
- **Do NOT flag**: Snapshots retained under explicit long-term legal hold or active compliance backup schedules.
- **Remediation**: Clean up obsolete orphaned snapshot via `kind: gcloud`.

A project holding no snapshots at all gets the same plain `checks_not_applicable` entry §2.4 describes, for the same reason: `snapshots list` returned an empty array, so there is no snapshot whose source disk could be missing. Reporting that as a clean §2.5 claims a pass over a population that does not exist.

#### 2.6 Connection tracking table full on a GKE node (`node-conntrack-saturation`)

- **Severity**: `major`
- **Command**: `gcloud logging read 'resource.type="gce_instance" AND log_id("serialconsole.googleapis.com/serial_port_1_output") AND textPayload:"nf_conntrack: table full, dropping packet" AND labels."compute.googleapis.com/resource_name"=~"^(?:(?:$NODE_GROUPS)-[a-z0-9]{4})$"' --project=$PROJECT --freshness=24h --limit=1000 --format=json`, where `$NODE_GROUPS` is the base name of each listed node pool's instance group (a node's name without its last hyphen and four characters), joined with `|`; a node with no pool label, or whose name is not of that form, is added by its whole name, and the word `timestamp` in a name is written `timestam[p]`, because `gcloud` adds no `--freshness` bound to a filter that holds the word. The collector also drops a returned line that is older than the 24 hours, by the line's own time.
- **Condition**: In the last 24 hours the serial console log of a GKE node carries the kernel's line `nf_conntrack: table full, dropping packet`. The kernel writes it when the node's connection tracking table is full and it drops a new connection. One finding names each node pool with such lines: the cluster, the pool, the nodes, the number of lines, and the first and last time. Its object is `NodePool/<location>/<cluster>/<pool>`. The lines of a node that a repair or the autoscaler deleted since count for its pool: the node's name still carries the base name of the pool's instance group. Two node pools whose objects are spelled alike inside a finding id (cluster `prod` with pool `web-default`, and cluster `prod-web` with pool `default`) are one finding that names both. A node whose instance carries no node pool label gets a finding of its own.
- **Do NOT flag**: A table that is filling but not full. The check reads the kernel's line and nothing else; GKE collects no figure for the table by default, so no finding states a share of `nf_conntrack_max`. The number of lines is not the number of dropped packets, because the kernel limits how often it writes the line. An instance that is no node of a listed node pool: the read asks only for the node pools that the collector listed, so it returns no line of a VM that is no GKE node, of a node pool with no running node, or of a cluster that the install's scope excludes.
- **Remediation**: Add nodes to the pool so that each node carries fewer workloads and connections, raise `net.netfilter.nf_conntrack_max` and `net.netfilter.nf_conntrack_buckets` in the node pool's system configuration (`linuxConfig.sysctl`; GKE 1.32.0-gke.1448000 or later, and `nf_conntrack_max` between 65536 and 4194304), or enable NodeLocal DNSCache to cut the entries that DNS lookups take.

The check sees a node only when the node's serial console reaches Cloud Logging. The instance metadata `serial-port-logging-enable: "true"` turns that on, and GKE sets it on its nodes. A quiet node writes nothing to that log for days, so the absence of entries shows nothing about one node; the collector reads the metadata value instead. A project with no running GKE node gets a `checks_not_applicable` entry. A first read that fails, or a control read that fails, puts the slug in `checks_unevaluated`, with the plain cause in `limitations`; the failed command and its error text are in the `checks_unevaluated` reason only, and no node pool is then listed in `noted_objects`. The two states below last as long as a setting of the customer lasts, so the collector treats them as §2.1 treats a rotated console: the check still ran, the slug is not in `checks_unevaluated`, and the target's `notes` says what the check could not see. The first state is a node with the metadata value off: the note names the node pools of those nodes in words (at most five, then "and N more"), not the nodes (a node with no pool label is named itself), and the check keeps its verdict for the nodes that send the log. The second state is a project that stores none of that log. The metadata value does not say that the project keeps the log: an exclusion on its log sink, a sink to another project, or the organization policy `compute.disableSerialPortLogging` stores no line. So on a day with no kernel line the collector asks for any one serial console entry of the node pools of its first read in the last 30 days; with none, the note says that nothing shows the log reaches Cloud Logging. The control sees a stop of that log's storage up to 30 days late: an older entry still answers it. It is one read, so an exclusion that drops the console of one cluster and not of another is not seen. In both states `noted_objects` lists each node pool that the run could not see, but not a pool with a finding in this run. Do not report those node pools as clean. A previous finding on one of them did not go away in this run's sight: carry it forward as §2.1 says, and do not write it in `resolved_because`; `finish` refuses that entry. Relay the `report_notes` lines as §2.1 says. One read names at most 25 instance groups and returns the newest 1,000 lines. In a project with more than 250 groups a read names more, up to 250, so that ten reads ask for every group of up to 2,500. While a page is full, the collector reads again for the groups that the page did not name, up to five reads for one set of groups and twenty reads for the project, so the lines of one busy node pool do not hide another; every count in a finding is then a floor ("At least"), and a finding's evidence command is the first read that returned a line of it. A node pool that no read reached, because the pages were full of other pools' lines, the project has more groups than twenty reads ask for, or a later read failed, is named by number in a `limitations` sentence and listed in `noted_objects`; the findings from the pages that were read stand. A noted node pool carries a `command` only where the read of the logging switch says why. The target's `commands` record shows the check's first reads whole and counts the rest; a read that failed is in the collector's log and not in the record.

A finding stands while a run finds such lines in its last 24 hours. A run with none drops it, and the report then calls it resolved. That says that the kernel wrote no such line that day; it does not say that the cause is fixed. The window is the audit's daily cadence: a run that starts later in the day than the run before it leaves a gap that neither run reads, and a day with no run is not read at all.

### 2a. Check declared intent before reporting a posture

One check judges a **posture** — a retained object an owner may keep on purpose — rather than a fault: 2.5 `orphaned-snapshots`. Its own _Do NOT flag_ clause names a legal hold or a compliance retention this audit cannot read, which is why every candidate comes back `needs_triage: retention-hold`. A snapshot kept after its source disk is gone — a legal hold with a review date, a pre-migration copy, a compliance baseline — is a choice an owner may declare, and the waste audit already lets an owner declare the same shape for a kept disk. Every other check in §2 is a fault no declaration excuses: a startup script that failed on the last boot, a group creating and deleting instances at the same instant, a reservation at 90% with less than one host free, a node whose kernel dropped connections on a full table. Each of those declared in a repository is a declared bug, and it stays a finding.

The step is the one the obtainability SOP's §4a describes, and the harness does its half the same way here:

- **The harness searches first.** `start` reads every repository in `declared_intent_repos` (the GitOps repository plus each `context_repos` entry) for OKF notes whose frontmatter carries a `declares:` list, files what it finds at `declarations_path`, and lists each repository it read completely under `declared_intent_searched`. `finish` moves every 2.5 finding a filed declaration covers to `declared` (§4) itself. A candidate a note covers is still written to `findings`: `finish` is what moves it, and a candidate you leave out because you found the note yourself is declared by `finish` from the collector's manifest when a note covers it, without your evidence.
- **What counts as a declaration.** An item `{check, namespace, object}`, optionally `cluster` and `expires`, with `check` the slug `orphaned-snapshots`, `namespace` the empty string written out as `namespace: ""` (the finding sits on a project target with no namespace; an omitted key or a YAML null is skipped with a WARNING, not read as empty, and a namespace written on the item, or on the finding, is read as empty) and `object` as the finding names it: `Snapshot/<name>`, the bare snapshot name (a global object, no zone segment), never a `Disk/` or a `Project/` — the validator rejects a `declared` entry whose object is anything but a `Snapshot/`. `cluster`, when present, is the finding's `project/<project-id>`; omitted, the item is fleet-wide and covers every project whose snapshot matches. The match is case-blind on those fields, scoped `(check, cluster, namespace, object)` first and fleet-wide second. An item with any other slug, a missing field or an `object` that is not `Kind/name` is skipped with a WARNING (an item that belongs to another stream is noted, not warned about), and the rest of the note still counts. Body text is never read. `expires` is the last day the item stands (`YYYY-MM-DD`, UTC), the natural fit for a legal hold with a review date; what a lapsed item does is the obtainability SOP's §4a: the posture stays a finding, with a _Declared intent expired_ line naming the note and the date.
- **Where the harness looks**, how it records the commit it read, what you do for a repository it could not read, and what a search you could not complete means are the obtainability SOP's §4a, word for word: a repository `start` read completely needs nothing from you; one it could not read is one you copy, grep and record yourself under `declared_intent_searched` as `owner/name@sha`, or leave as the coverage gap it is. Never write a `declared` entry for a declaration you did not read.
- **A step you skipped is visible, not silent.** Whenever any project target's `checks_run` names 2.5, `finish` requires `declared_intent_searched` to cover every slug in `declared_intent_repos`. Anything less is no search: `finish` withholds every 2.5 finding, the other four checks publish, the ledger names each withheld posture under _Declared intent not searched_, and the run is `partial`.

### 3. Generate remediation artifacts

For promoted findings requiring `kind: manifest` remediation, write the updated Terraform or manifest file to `remediation.path` resolved within the `workspace` GitOps repository:

- Discover the target configuration from existing repository paths (e.g., `terraform/modules/compute/vm.tf`).
- **The fleet-audit skill's rule that "a `path` is discovered, never invented" (`/opt/data/profiles/platform/skills/fleet-audit/references/findings-document.md`) decides where the file goes** — for an object the repo already declares and for one it does not yet. That rule anchors a sibling on the cluster and namespace; every target this audit reports on is project-scoped, so here the sibling that proves a directory is reconciled is another declaration governing that same project: a Config Connector `Compute*` resource (a `ContainerNodePool` for a 2.6 node pool), or the Terraform file that already describes the instances, MIGs, node groups or node pools you are fixing. Find no sibling for the project and the finding is `kind: manual`.
- Never write to a directory outside the reconciled GitOps hierarchy. Creating an object the repo does not yet declare is permitted and is what makes a finding resolvable by a pull request; inventing the _directory_ to put it in is not, because the repository is reconciled over a fixed set of paths and a file outside them is applied by nothing.

### 4. Emit findings.json

Fill the drafted document at `findings_path` in place, as step 3 of the skill says and never as one tool call that carries the whole document, with `audit: "gce-compute-fleet-audit"`, `scope.clusters` listing every target you queried — each carrying the `checks_run` list §1 required and, where §1 recorded them, that target's `checks_not_applicable` entries and `limitations` string — and `scope.skipped` listing only the targets you could not read.

`command` in `checks_run` is the literal inspection command executed, and anything under eight characters is rejected.

Every finding must conform to the full findings schema:

```json
{
  "audit": "gce-compute-fleet-audit",
  "scope": {
    "clusters": [
      {
        "name": "project/proj-1",
        "location": "global",
        "project": "proj-1",
        "checks_run": [
          {
            "check": "gce-startup-script-status",
            "command": "gcloud compute instances get-serial-port-output vm-1 --zone=us-central1-a --port=1 --project=proj-1"
          }
        ]
      }
    ],
    "skipped": []
  },
  "findings": [
    {
      "check": "gce-startup-script-status",
      "severity": "critical",
      "title": "Startup script failure on standalone instance vm-1",
      "cluster": "project/proj-1",
      "namespace": "",
      "object": "ComputeInstance/us-central1-a/vm-1",
      "impact": "Instance vm-1 failed initialization and is unable to serve production traffic.",
      "evidence": {
        "command": "gcloud compute instances get-serial-port-output vm-1 --zone=us-central1-a --port=1 --project=proj-1",
        "excerpt": "startup-script exit status 1"
      },
      "recommendation": {
        "action": "Fix failing package dependencies in instance startup-script metadata.",
        "rationale": "Prevents boot failure and restores automated instance recovery.",
        "risk": "Requires instance reboot to apply updated startup script."
      },
      "remediation": {
        "kind": "gcloud",
        "path": "",
        "note": "gcloud compute instances reset vm-1 --zone=us-central1-a --project=proj-1"
      }
    }
  ]
}
```

What §2a adds:

- `declared_intent_searched`: a top-level list of `owner/name@sha` strings, one per repository §2a searched. Required in effect whenever any project target's `checks_run` names 2.5: together with the list `start` printed under the same key, it must cover every slug `start` printed under `declared_intent_repos`, or `finish` withholds every 2.5 finding as a coverage gap (§2a). Omit the key only on a run where 2.5 ran nowhere.
- `declared`, optionally: one entry per snapshot you moved out of `findings` yourself — `finish` adds the entries for the notes `start` filed, and declares a collector candidate you left out of the document when a note covers it, so omitting a declared snapshot does not hold it on the ledger — each carrying `check` (2.5 only), `cluster` (the `project/<project-id>` target), `namespace` (`""`), `object` (`Snapshot/<name>`), `title` and `declaration` with `repo`, `path` and `excerpt`. No severity, no remediation, no id; a declared fault exits 2 and publishes nothing.

### 5. Close the audit run

```bash
python3 /opt/data/profiles/platform/skills/fleet-audit/scripts/audit_report.py finish --audit gce-compute-fleet-audit \
  --findings-file /opt/data/scratch/findings_gce-compute-fleet-audit.json \
  --manifest-file /opt/data/scratch/manifest_gce-compute-fleet-audit.json
# -> {"status":"CLEAN"|"HELD"|"OPENED"|"UPDATED"|"NO_FORGE"|"STORE_LEDGER","issue_url":...,"new":n,"resolved":m,
#     "prs_opened":[...],"prs_closed":[...],"partial":false,"coverage_gaps":[],
#     "silent_ok":true}
```

- On a **scheduled** run, `silent_ok: true` -> your final response is exactly `[SILENT]`.
- **An on-demand run is never silent.** `finish` returns `silent_ok: true` only for a scheduled run. On a run that a person asked for, `silent_ok` is always `false`, so report the outcome and the ledger URL.
- `status == "NO_FORGE"` → no forge is configured, or the broker cannot reach the repository, so nothing was published and there is no ledger URL: unless the run is silent by the rule above, your report is `summary` verbatim (the skill's "No forge configured" section, `/opt/data/profiles/platform/skills/fleet-audit/references/finish.md`).
- `status == "STORE_LEDGER"` → the forge has no issue tracker, or no labels, so the ledger is in the report store and there is no ledger URL: unless the run is silent by the rule above, your report is `summary` verbatim, which names the remediation pull requests (the skill's "A forge with no issue tracker" section, `/opt/data/profiles/platform/skills/fleet-audit/references/finish.md`).
- Repo writers can trigger remediation by commenting `/remediate <finding-id>` or `/remediate all` on the ledger issue.
- `partial` is `true` when the run could not read the whole scope: any target in `scope.skipped`, any target kept in scope with a `limitations` note, or a §2a search that did not cover every repository in `declared_intent_repos`, which withholds every 2.5 finding and names each under _Declared intent not searched_. `coverage_gaps` names each one in a sentence.

---

## Red Lines

- **Read-only audit.** Never terminate Compute Engine instances, delete Persistent Disks, or modify live firewall rules.
- **No hand-written issues or PRs.** `audit_report.py` owns the entire git and forge write path.
- **Never print raw credentials.** Secret tokens, certificates, private keys, or credentials in serial port output must never reach an excerpt.
- **No unstable finding identity.** Name the durable resource identifier (`ComputeInstance/<zone>/<name>`, `ManagedInstanceGroup/<region>/<name>`, `NodePool/<location>/<cluster>/<pool>` for 2.6, whose target is the project and not a cluster), never an ephemeral instance ID. The zone or region is part of the identity, not decoration: a GCE instance name is unique per zone, so two `web-1`s in one project collapse to one finding id without it and `finish` refuses the whole document over the collision.
- **Never emit a manifest that directly deletes a VM or disk.** Deletion remediations are `kind: manual` or `kind: gcloud` only.
- **Never export internal VM secrets or private keys in issue bodies.**
