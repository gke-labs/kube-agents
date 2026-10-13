# SOP: GCE Compute Engine and MIG Fleet Audit (Daily Governance)

**Purpose:** Sweep all managed GCE Compute Engine instances and Managed Instance Groups (MIGs) across target GCP projects for failed startup scripts, MIGs that cannot converge on their target size, sole-tenant headroom exhaustion, and orphaned storage snapshots. The question this audit answers for a platform admin is: _which standalone VMs or MIG instances have failed startup scripts, which MIGs are stuck in a resize loop, and which storage snapshots belong to deleted disks?_ Output is this stream's single GitHub ledger issue, rewritten in place on every run, plus narrow remediation Pull Requests carrying Terraform or manifest fixes for the findings that get promoted.

**Cron:** id `gce-compute-fleet-audit`, schedule `45 7 * * *` (daily 07:45 UTC).

**Data sources:** `gcloud compute instances ...`, `gcloud compute instance-groups ...`, `gcloud compute resource-policies ...`, and `gcloud compute snapshots ...`, run once per project in the resolved project scope (§1).

---

## Execution Checklist

### 0. Open the audit run

```bash
python3 ./skills/fleet-audit/scripts/audit_report.py start --audit gce-compute-fleet-audit
```

Returns `{"issue": <int|null>, "repo":"org/repo", "workspace":"/opt/data/gitops/gce-compute-fleet-audit/org__repo", "findings_path":"/opt/data/scratch/findings_gce-compute-fleet-audit.json", "pending_remediation_requests": [<finding_id>, ...]}`.

If `pending_remediation_requests` is non-empty, inspect each requested finding in the open issue and write the updated manifest or Terraform file to `workspace` at `remediation.path` before proceeding to step 3 (`finish`).

### 1. Enumerate the target fleet

**Resolve the project scope first.** The scope is the host project (`gcloud config get-value project`) plus every project `gcloud projects list --format="value(projectId)"` returns. Run every collection command once per project, passing `--project` explicitly — the ambient default silently audits one project and reports the result as a fleet sweep. The scope is what the agent's identity can read, so an operator narrows it by narrowing the IAM grant. A listing that exits non-zero, or that returns without the host project, cannot say how many other projects exist: sweep the projects you have and add one `scope.skipped` entry, `{"cluster": "project/UNENUMERATED_PROJECTS", "reason": "<the listing's rc and stderr excerpt, or the host project it omitted>"}`, so the run publishes as partial rather than as the whole fleet. A run narrowed on request — someone asks for one project, or a helper script is given `--project-id` or `MONITORED_PROJECT_IDS` — records the same entry with the reason `scope narrowed to <projects> on request`: it read no other project, and without the entry `finish` resolves every ledger finding on a project the run never looked at. A project where the API this audit reads is disabled (`SERVICE_DISABLED`, `accessNotConfigured`, `has not been used in project`) holds nothing to audit and counts as empty, not skipped: recording it as a loss would pin every run partial for as long as the project exists. That holds only when the refusal names this project — its id, or the number `gcloud projects describe <project> --format='value(projectNumber)'` prints. A refusal naming another project, such as the credential's quota project, says nothing about this one: record it in `scope.skipped` as a failed read. The collector (§2) applies all of this itself: it leaves out a project whose own API is off; one holding no instance, MIG, node group or snapshot is listed with all four checks in `checks_not_applicable`, which `finish` takes as fully accounted for; and hands back `project/UNENUMERATED_PROJECTS` as a `gate-failed` target carrying the reason.

- **Pass `--project` explicitly on every collection command.** Never rely on the ambient default: it silently audits one project and reports the result as a fleet sweep.
- Record each resolved project as `{name: "project/" + project_id, location: "global", project: project_id, checks_run: [...]}` into `scope.clusters`. The `project/` prefix is what makes the scope line say "project"; a hyphen there is read as a bare cluster name.
- **`checks_run` is mandatory on every scope entry:** Each entry is an object `{"check": "<slug>", "command": "<literal command>"}` naming the exact inspection command executed on that project target.
- **This audit sees the Standard-mode fleet only.** The Compute Engine API does not return a GKE Autopilot node's `gk3-*` instance, its managed instance group, or its boot disk to the Platform Agent identity — a 404 rather than a 403, [by design](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/autopilot-architecture) rather than for want of a grant. That is the correct universe: Google manages those nodes, so §2.1's startup scripts and §2.2's convergence are not the operator's to set or to fix, and a finding on one would carry a recommendation nobody can act on. Do not record it as a coverage gap or a `limitations` note — this stream's only target is the project, so a gap on it holds every GCE finding open forever. The case that does need declaring is a project whose nodes are all Autopilot: it enumerates empty, and the collector declares §2.1 and §2.2 in `checks_not_applicable` rather than letting an unseen fleet read as clean.
- A project or target you cannot reach goes in `scope.skipped` as `{"cluster": "project/" + project_id, "reason": "<why>"}`. The key is `cluster`, not `name` — a skipped entry keyed the way the `scope.clusters` entry above is keyed fails validation and `finish` publishes nothing, which discards the whole run at exactly the moment part of the fleet was unreadable. The sweep continues — one project's permission error never decides the outcome for the rest of the fleet. If a target is partially readable, record the refusal in its `limitations` string. Declare structurally inapplicable checks in `checks_not_applicable`.

### 2. Diagnostic checks roster

**Run the collector before evaluating any check below by hand.**

```bash
python3 ./skills/gce-compute-fleet-audit/scripts/compute_fleet_audit.py > /opt/data/scratch/manifest_gce-compute-fleet-audit.json
```

This stream's targets are GCP projects, not GKE clusters, so its collector is its own script rather than `fleet-audit`'s `collect.py` — see the script's own module docstring for the field contracts it assumes of each `gcloud` command's JSON. Pass `--project-id <id>` only to scope a run to one project. Left alone it resolves the scope §1 describes — `MONITORED_PROJECT_IDS` when set, which narrows the run on purpose, and otherwise the configured project plus every project `gcloud projects list` returns — so an identity that can list the organisation sweeps every project it can see rather than the one it is configured for. Read the manifest before doing anything else:

- Every entry in `manifest.clusters` is one project target, named `project/<project_id>`, carrying one `outcome`. `"collected"` means every check the collector implements already ran; do not re-run it by hand. `"gate-failed"` means a project-level read the whole target depends on failed (an instance listing that did not complete is the usual one): put it in `scope.skipped` with its `error` as the reason. A failed read of one instance's console is not that, and stays on a `"collected"` target as a `limitations` note. `project/UNENUMERATED_PROJECTS` is always `gate-failed`: it stands for the projects the run did not enumerate, so it goes in `scope.skipped` the same way. A manifest with a top-level `error` read no project at all: do not call `finish`, and report the error as your one-line summary. The collector exits non-zero for it.
- For a `"collected"` target, copy its `commands` list into that target's `checks_run` — minus any entry whose `check` that same target also lists in `checks_not_applicable` or `checks_unevaluated`. A `commands` entry records that a command ran, not that the check reached a verdict on that target, so one read is routinely recorded against slugs it could not answer for; `finish` rejects a `checks_run` naming a slug the collector declared inapplicable there. Copy that target's `checks_not_applicable` and its `limitations` string verbatim too.
- **The roster is four checks and the collector implements all four**, so there is nothing below for you to hand-run. §2.3 is a numbered slot rather than a check: `ops-agent-guest-health` is not on the roster and `finish` rejects a `checks_run` or a `finding.check` naming it. Where a target does carry a `checks_not_applicable` entry, the list you publish is the collector's, unchanged: copy it, and where a target has none, publish none.
- **`checks_not_applicable` and `checks_unevaluated` mean different things**, and neither is yours to rewrite. A check in `checks_not_applicable` could not apply — the project reserves no sole-tenant node groups, so §2.4 has no reservation to measure. A check in `checks_unevaluated` applied but reached no verdict: the collector read the surface and no figure came back, or every read the check depends on failed (every qualifying console refused, say). Put it in neither `checks_run` nor `checks_not_applicable`; the target's `limitations`, which you copy, names it, and that makes the run partial so its findings are not called resolved off this run. `finish` refuses a document that files it either way.
- Every entry in a `"collected"` target's `candidates` is a verified finding: `check`, `object`, `severity`, `impact` and `excerpt` are already computed, and `finish` overwrites your `evidence` with the collector's. What is still yours to write is the `title` and the `recommendation`, and for a `kind: manifest` remediation the Terraform or manifest file itself (§3).
- **A candidate carrying `needs_triage` is the one place your judgment still decides whether it ships.** The collector applies the mechanical condition and not the check's _Do NOT flag_ clause, so it names the exclusion it could not apply. There are four, and what you do with one depends on whether you can settle it. `gke-managed-node` (§2.1) means confirm the instance is not a GKE node pool member, and `gke-managed-mig` (§2.2) means decide whether the churn is pod-driven; both are readable, so read them and drop the candidate where the exclusion holds. `retention-hold` (§2.5) and `maintenance-window` (§2.4) name surfaces this audit cannot reach at all. **An exclusion you cannot read is not an exclusion you have established: publish, and put the check the reader still owes into the recommendation.** Dropping on a suspicion is how a stream comes to report a clean fleet over findings it never disproved. `needs_triage` is not a findings-schema field — do not copy it into the document.
- Pass `--manifest-file <path>` to `finish` (§5) so it cross-checks your `checks_run` against what the collector actually ran.

#### 2.1 Instance startup script failures in serial port output (`gce-startup-script-status`)

- **Severity**: `critical`
- **Command**: `gcloud compute instances get-serial-port-output $VM --zone=$ZONE --project=$PROJECT --port=1`
- **Condition**: VM serial port console output contains a fatal startup script error: `Script "startup-script" failed with error` (the current guest agent), or `startup-script exit status <non-zero>` or `Finished running startup scripts with error` (older agents).
- **Do NOT flag**: GKE node pool instances managed directly by GKE control plane or instances cleanly completing boot without errors.
- **Remediation**: Correct boot metadata or deployment configuration in instance template or Terraform definition.

The collector reads only the consoles of instances that run a startup script at all — its own metadata sets `startup-script` or `startup-script-url`, or the project's common metadata does and therefore every instance's does. Where no instance qualifies it declares the check in `checks_not_applicable` rather than reporting it clean. All three markers above are printed by `google_metadata_script_runner`, which does not run when no script is set, so a console with no script behind it cannot carry any of them however badly the instance booted; reading such consoles and finding nothing is a pass that was never capable of failing. A fleet of GKE Standard nodes is exactly that shape: they bootstrap from `user-data` and `kube-env` and set neither startup-script key. Read "can see" strictly: an Autopilot node does set `startup-script`, to a Google-installed stub that neutralises the hook, so the generalisation holds for the Standard fleet rather than for GKE nodes as a class. It changes nothing about what the check should do — the scope note above already puts those nodes outside this audit's universe, and they never reach the count the `checks_not_applicable` reason quotes. A `compute project-info describe` that fails leaves every RUNNING instance a target, because skipping the check across a whole project on the strength of a read that did not happen is the worse error.

#### 2.2 Managed Instance Group convergence stalled (`mig-convergence-stalled`)

- **Severity**: `major`
- **Command**: `gcloud compute instance-groups managed list --project=$PROJECT --format=json`
- **Condition**: A group's `currentActions` shows `creating` and `deleting` both non-zero: it is adding and removing instances at the same moment, which is a resize loop and not a scale event.
- **Do NOT flag**: GKE node pool groups (`gke-` or `gk3-` prefix) undergoing standard pod-driven scale events. The collector cannot tell a pod-driven churn from a pathological one, so it hands these back carrying `needs_triage: gke-managed-mig`. A group with an update in progress (`status.versionTarget.isReached: false`), because a rolling update with surge or a GKE surge upgrade creates and deletes at once by design; the collector skips these itself. Also do not flag `status.isStable: false` on its own: instability is the normal state of any group mid-scale.
- **Remediation**: Adjust the autoscaling cool-down period and utilization targets in the MIG specification.

This slug replaced `mig-autoscaler-flapping`, whose condition was a rate — repeated resizes inside fifteen minutes. No `gcloud` read carries a MIG's resize history to count one, so the rate was never measurable; a resize loop caught in the act is the part of that intent a single point-in-time read can establish, and the slug now says what it measures. A group whose no-retry creation failed is not measurable either: `creatingWithoutRetries` counts creations still to be tried, and a failed one lowers `targetSize`, so the group reads as converged on its smaller target. On a ledger that already carries a `mig-autoscaler-flapping` or `ops-agent-guest-health` finding, the first run after this roster change reports it resolved: neither slug is checked any more, so nothing re-examines it.

#### 2.3 Compute Engine Ops Agent guest telemetry (not audited by this stream)

There is no `ops-agent-guest-health` slug on this stream's roster, and `finish` rejects a `checks_run` or a `finding.check` naming one. Do not hand-run this check and do not publish a `checks_not_applicable` entry for it either.

It is recorded here because the gap is deliberate rather than forgotten. Whether a guest's Ops Agent is reporting lives in Cloud Monitoring or in OS Config inventory, and neither surface is reachable: `gcloud monitoring` exposes no metric read the credential proxy's allowlist could carry, and `osconfig.googleapis.com` is not enabled on the reference install. Carrying the slug as permanently unevaluated was worse than dropping it: it would make every run partial, so one unimplementable check would keep every finding on the other three from ever being announced resolved and their remediation pull requests from ever closing. If the surface becomes reachable, this section is where the check goes back.

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

### 3. Generate remediation artifacts

For promoted findings requiring `kind: manifest` remediation, write the updated Terraform or manifest file to `remediation.path` resolved within the `workspace` GitOps repository:

- Discover the target configuration from existing repository paths (e.g., `terraform/modules/compute/vm.tf`).
- **The fleet-audit skill's rule that "a `path` is discovered, never invented" decides where the file goes** — for an object the repo already declares and for one it does not yet. That rule anchors a sibling on the cluster and namespace; every target this audit reports on is project-scoped, so here the sibling that proves a directory is reconciled is another declaration governing that same project: a Config Connector `Compute*` resource, or the Terraform file that already describes the instances, MIGs, or node groups you are fixing. Find no sibling for the project and the finding is `kind: manual`.
- Never write to a directory outside the reconciled GitOps hierarchy. Creating an object the repo does not yet declare is permitted and is what makes a finding resolvable by a pull request; inventing the _directory_ to put it in is not, because the repository is reconciled over a fixed set of paths and a file outside them is applied by nothing.

### 4. Emit findings.json

Write the whole document to `findings_path` in one shot, with `audit: "gce-compute-fleet-audit"`, `scope.clusters` listing every target you queried — each carrying the `checks_run` list §1 required and, where §1 recorded them, that target's `checks_not_applicable` entries and `limitations` string — and `scope.skipped` listing only the targets you could not read.

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

### 5. Close the audit run

```bash
python3 ./skills/fleet-audit/scripts/audit_report.py finish --audit gce-compute-fleet-audit \
  --findings-file /opt/data/scratch/findings_gce-compute-fleet-audit.json \
  --manifest-file /opt/data/scratch/manifest_gce-compute-fleet-audit.json
# -> {"status":"CLEAN"|"HELD"|"OPENED"|"UPDATED","issue_url":...,"new":n,"resolved":m,
#     "prs_opened":[...],"prs_closed":[...],"partial":false,"coverage_gaps":[],
#     "silent_ok":true}
```

- On a **scheduled** run, `silent_ok: true` -> your final response is exactly `[SILENT]`.
- **An on-demand run is never silent.** If a person dispatched this job, report the outcome and the ledger URL whatever `silent_ok` says.
- Repo writers can trigger remediation by commenting `/remediate <finding-id>` or `/remediate all` on the ledger issue.

---

## Red Lines

- **Read-only audit.** Never terminate Compute Engine instances, delete Persistent Disks, or modify live firewall rules.
- **No hand-written issues or PRs.** `audit_report.py` owns the entire git and forge write path.
- **Never print raw credentials.** Secret tokens, certificates, private keys, or credentials in serial port output must never reach an excerpt.
- **No unstable finding identity.** Name the durable resource identifier (`ComputeInstance/<zone>/<name>`, `ManagedInstanceGroup/<region>/<name>`), never an ephemeral instance ID. The zone or region is part of the identity, not decoration: a GCE instance name is unique per zone, so two `web-1`s in one project collapse to one finding id without it and `finish` refuses the whole document over the collision.
- **Never emit a manifest that directly deletes a VM or disk.** Deletion remediations are `kind: manual` or `kind: gcloud` only.
- **Never export internal VM secrets or private keys in issue bodies.**
