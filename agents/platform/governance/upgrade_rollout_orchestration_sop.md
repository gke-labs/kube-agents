# SOP: Upgrade Rollout Orchestration (On Request)

**Cron id:** none. This SOP runs when a user asks to plan or run an upgrade across cluster families, or with a RolloutSequence: "roll out 1.34 to the fleet", "upgrade the prod clusters one family at a time", "create a rollout sequence for the prod fleet", "can the rest follow the canary". No scheduled job starts it.

**Purpose:** Upgrade one cluster per family first, read what the upgrade did to it, and let the rest of the family follow only on a clean diff. Three facts from one production fleet shape the order. A logging agent's bug reached every cluster at once because no cluster went first. An upgrade raised a dataplane agent's log verbosity and the logging bill, and nothing compared the week after with the week before. A sequence creation failed for two days and nothing named the manual path; four days later a second sequence was refused, because the platform allows one RolloutSequence per fleet host project. So the order is: the precheck, the plan with one canary per family, the soak, the diff with its gate, the rest; and when the sequence cannot be created, the same order per cluster by hand.

**Data sources:** the platform_control `fleet_scope` tool; `gcloud container clusters list --format=json` (the `resourceLabels.upgrade-family` label and the `fleet.project` block of each cluster record); `gcloud container get-server-config` and `gcloud container operations list`; `gcloud container fleet rolloutsequences list` and `gcloud container fleet rollouts list` (read-only; `roles/gkehub.viewer` on the fleet host project); the readiness report `/opt/data/profiles/platform/skills/fleet-upgrade-verification/scripts/fleet_upgrade_report.py --readiness`; the upgrade retrospective's collector `/opt/data/profiles/platform/skills/fleet-audit/scripts/upgrade_retrospective.py` and its store `/opt/data/upgrade-retrospective/reports/`; the `gke-upgrades` skill's `/opt/data/profiles/platform/skills/gke-upgrades/references/runbook-template.md` for the manual path; the `submit-suggestion` skill for every change that is a pull request. Nothing else.

---

## Execution Checklist

### 0. What this SOP writes, and what it never writes

This SOP produces a plan, a stage configuration, pull requests and operator steps. It runs no upgrade. The operator creates the RolloutSequence, starts the rollout, pauses or resumes it, and runs every manual upgrade; the agent quotes each of those commands verbatim as an operator step and never runs one. Every change to a cluster's own record (a label, a version floor, a release channel, a maintenance exclusion) is a pull request through the `submit-suggestion` skill when the cluster is in a managed GitOps repository, and an operator step otherwise. The red lines at the end restate this.

Two questions are not this SOP. "Is cluster X ready" and "what would stop the upgrade" are the `fleet-upgrade-verification` skill's readiness report. "What did the last upgrades do" and "is there an upgrade problem to fix" are section 7 of `/opt/data/profiles/platform/governance/upgrade_retrospective_sop.md`. This SOP reads both and answers a third question: in what order, and may the rest follow.

### 1. Inputs

Collect these before any plan, and say which the request left out:

1. **The target version**, or the release channel's default when the request names none; `gcloud container get-server-config --location <location> --format=json` lists what each channel offers.
2. **The families.** Call the platform_control `fleet_scope` tool first; its `projects` are the fleet. Then, per project, `gcloud container clusters list --project <p> --format=json`. A family is the value of the cluster label `upgrade-family` in `resourceLabels`; its members are every cluster in scope with that value. A cluster with no `upgrade-family` label is a family of one, under its own name, and is listed as such. A request that names clusters rather than families is read as the families those clusters belong to; a request that names a family that no cluster carries is answered with the family values that exist.
3. **The fleet host project of each member**: the `fleet.project` block of its cluster record. A cluster with no `fleet` block is not a fleet member: no RolloutSequence can carry it, and its upgrade is the manual path of section 6. Say so per cluster.
4. **The sequence name the plan would create**, when the request gives one; otherwise the plan proposes `<family or fleet>-<target minor>`.
5. **Each family's blocking findings.** Run the readiness report once for the fleet, as the `fleet-upgrade-verification` skill says: `python3 /opt/data/profiles/platform/skills/fleet-upgrade-verification/scripts/fleet_upgrade_report.py --readiness <collector_args from fleet_scope, verbatim> --output /opt/data/scratch/fleet_readiness_$(date +%s)_$$.json` with the terminal's `timeout: 600`. Per member, read `readiness.status` (`blocked`, `ready`, `unknown`), `readiness.rule_blocking[]` and `readiness.risks[]` from that file. These decide the canary (section 3) and name what the plan waits on; do not grade a cluster yourself.

### 2. The precheck, before the plan

The platform allows one RolloutSequence per fleet host project. A plan that creates a second is refused at creation (`FAILED_PRECONDITION`), days after the plan was agreed. So read the host project before the plan names a sequence, once per host project in the plan:

```bash
gcloud container fleet rolloutsequences list --project <host project> --format=json
```

`rolloutsequences` is one word. The terminal's command policy admits this read in that spelling (`list` and `describe`, and `gcloud container fleet rollouts list`) and no other form under `fleet`: a refusal that names `policy rule: gcp.read-only` (exit 77) means the command as typed is not one of them, most often `rollout-sequences`, and the refusal names the nearest admitted read: retype it as named. A missing role or a disabled API comes back from gcloud itself (`PERMISSION_DENIED`, `SERVICE_DISABLED`) and is the refused-or-failed case below. The precheck is this listing, made in this turn: a REST read in its place, a count without the sequences' names, or a listing carried over from an earlier card is not a precheck, and the answer names every sequence the listing returned, with its stages.

The rule, in the readiness report's own words (its rule `rollout-sequence-precheck` prints the same read as a risk row, filed on the host's first member; quote that row when the report of step 1.5 is at hand):

- A listed sequence, and a plan that names a new one: **blocked**. The plan is rewritten as stages added to the existing sequence (its name, its stages, its soak and selectors quoted from the listing), or planned in a separate host project. Say which.
- A listed sequence, and a plan that adds stages to it: **clear**.
- No sequence (`[]`): **clear**; the plan creates one.
- The read refused or failed (the API not enabled on the host, the role missing, a timeout): the precheck is **not done**, and the plan says so in place of the line below and stops at section 3's stage configuration; a plan whose precheck is not done is never handed over as ready.

Write the result as one declared line per host project, verbatim:

```text
rollout-sequence precheck: host <project>; existing <n>; plan <clear|blocked>
```

### 3. The plan: one cluster per family first

For each family, the **canary** is the member with `readiness.status: ready` and the fewest `readiness.risks[]`; when no member is `ready`, the member with the fewest `rule_blocking[]` rows, and its blockers are the plan's first prerequisites, quoted from the report with the rule's own remedy. Name the canary and the reason in one sentence per family. A family of one is its own canary.

A RolloutSequence selects clusters by label, not by name, and a stage's selector is one equality `resource.labels.<key>=='<value>'`, with a key the expression grammar accepts (letters, digits and underscores; a hyphenated key is refused as `cluster_selector is invalid`). So the plan proposes the label `upgrade_stage` on every member of the family: `<family>-canary` on the canary and `<family>-rest` on every other member (the key on every member, so the fleet consistency audit's label-keys facet sees no drift; `upgrade-family` stays the family's name for the collector and cannot carry a selector). The label is one pull request through the `submit-suggestion` skill editing `spec.resourceLabels` of each `ContainerCluster` in the managed repository, or, for a cluster outside it, the operator step `gcloud container clusters update <name> --location <location> --project <project> --update-labels upgrade_stage=<family>-canary` (or `-rest`), one per cluster.

The stage configuration the operator creates, one file per sequence, in the format `gcloud container fleet rolloutsequences create --stage-config` reads: a list of stages, each with `fleet-projects` and `soak-duration` (both required) and an optional `label-selector`; the keys sit at the item's level beside `stage:`, as the command's help shows. The API refuses a sequence whose fleet project has no catch-all stage, so the last stage carries no selector; it picks up a member that none of the labels name and does nothing for one already at the version:

```yaml
- stage:
  fleet-projects:
    - projects/<host project>
  soak-duration: 7d
  label-selector: resource.labels.upgrade_stage=='<family>-canary'
- stage:
  fleet-projects:
    - projects/<host project>
  soak-duration: 7d
  label-selector: resource.labels.upgrade_stage=='<family>-rest'
- stage:
  fleet-projects:
    - projects/<host project>
  soak-duration: 7d
```

The first stage's soak is seven days because the gate of section 4 compares the seven days after the upgrade with the seven days before; a shorter soak ends before the gate can read `clear`. One sequence carries several families as further stage pairs before the catch-all stage, in the order the request gives or, without one, the family with the fewest blocking findings first. The host project must hold a fleet: a project with none answers `fleet not found` at the sequence's creation, and creating the fleet is an operator step (`gcloud container fleet create --project <host project> --display-name <4 to 30 characters>`); the agent reads no fleet resource, since the proxy admits no read of it. The operator steps, quoted verbatim and never run here:

```bash
gcloud container fleet rolloutsequences create <sequence name> --project <host project> --stage-config <file>
gcloud container fleet rolloutsequences upgrade <sequence name> --project <host project> --control-plane-version=<target version>
gcloud container fleet rolloutsequences upgrade <sequence name> --project <host project> --node-version=<target version>
```

Hand the plan over in this order: the precheck line(s) of section 2, the families with their canaries and prerequisites, the label pull request or steps, the stage configuration, the three operator commands, and the sentence that section 4 decides whether the second stage runs.

### 4. The soak and the diff: the gate

When the canary's upgrade operations have ended (`gcloud container operations list --project <p> --filter 'operationType:(UPGRADE_MASTER OR UPGRADE_NODES) AND targetLink:<cluster>' --format=json`, the last `endTime`), wait until seven days after that end. Then run the retrospective's scoped diff on the canary, with no `start` and no `finish`:

```bash
python3 /opt/data/profiles/platform/skills/fleet-audit/scripts/upgrade_retrospective.py <collector_args from the fleet_scope tool, verbatim, or nothing on an install that declares no scope> --cluster <project>/<location>/<canary> --since <the first operation's startTime>
```

Read the canary's cluster block in the report it writes (`/opt/data/upgrade-retrospective/reports/<stamp>-scoped.md`, and `<stamp>-scoped.json` beside it with `families[]`): the `**Gate.**` line and the `**Log volume by container.**` lines. The gate is the collector's, never yours:

- `clear`: no new Error, no Warning that the upgrade brought, and no container whose Cloud Logging ingestion rose to at least 2.0 times the week before while reaching at least 1 GiB in the week after. The rest of the family may follow: the second stage runs when the soak ends, and nothing more is needed.
- `risk`: a new Error or Warning, or a container past both thresholds. The family holds: the operator pauses the rollout (`gcloud container fleet rollouts list --project <host project> --format=json` names it; `gcloud container fleet rollouts pause <rollout> --project <host project>`, an operator step). Quote the report's Errors and Warnings with their mitigations, and for a log-volume risk its one action: prepare the logging exclusion for that namespace and container (a pull request through `submit-suggestion`), or hold the patch for the family.
- `pending`: the after-week is not complete, a read failed, or the review was partial. Say when the gate can be read (the report names the time) and read it again then. A pending gate holds the family exactly as a risk does.

Write one declared line per family, verbatim:

```text
family <name>: canary <cluster>; gate <clear|risk|pending>; rollout <go|hold>
```

`go` only on `clear`; `hold` on `risk` and on `pending`. A hold is never bare: the sentence before the line quotes the report's entry for the gate, as the report writes it. For a log-volume risk that is the container (`<namespace>/Container/<name>`), its before-week and after-week bytes and the ratio; for an Error or Warning its heading and mitigation; for `pending`, the time the gate can be read. A line handed back without that sentence is not an answer, whoever asked.

### 5. The rest

The second stage upgrades the family's other members when the first stage's soak ends; `gcloud container fleet rollouts list --project <host project> --format=json` shows each stage's state (`PENDING`, `RUNNING`, `SOAKING`, `COMPLETED`, `PAUSED`). When a family's second stage has ended, run the same scoped diff on each of its members and report each gate, so a member that failed where the canary did not is named before the next family starts. Families proceed in the plan's order; a `risk` on any member of a family holds the families after it until the operator decides.

### 6. The manual path, when the sequence cannot be created

When the operator reports that creating the sequence failed (an outage of the platform's rollout service, a refusal the precheck did not predict), or chooses not to add stages to an existing sequence, say the failure once, in the plan and in chat, and keep the same order per cluster through the `gke-upgrades` skill's runbook steps (`/opt/data/profiles/platform/skills/gke-upgrades/references/runbook-template.md`):

1. The canary's control plane: a pull request through the `submit-suggestion` skill moving the `ContainerCluster`'s version or release channel in the managed repository; for a cluster outside it, the operator step `gcloud container clusters upgrade <name> --location <location> --project <project> --master --cluster-version <target version>`.
2. The canary's node pools: one operator step per pool, `gcloud container clusters upgrade <name> --location <location> --project <project> --node-pool <pool> --cluster-version <target version>` (there is no `node-pools upgrade` command).
3. Section 4 unchanged: seven days, the scoped diff, the gate line.
4. The rest of the family, the same two steps per cluster, only on `clear`; then the next family.

The manual path and the sequence path produce the same two declared lines.

### 7. Report in chat

The answer carries, in this order: the precheck line(s), one per host project; one family line per family, each hold with its reason quoted from the report; the canaries with their reasons and prerequisites; the operator steps and pull requests, each named as one or the other; and the time the next gate can be read. When the plan is a hand-over (sections 2 and 3 done, no upgrade run yet), every family line reads `gate pending; rollout hold` and the next sentence says the gate is read seven days after the canary's upgrade ends. Name the report each gate came from by its dated path under `/opt/data/upgrade-retrospective/reports/`. The answer is never `[SILENT]`.

### 8. What this SOP is not

It is not the readiness report, which grades one cluster against one target and is section 1.5's input. It is not the retrospective's on-demand route, which answers what the last upgrades did from the saved full report; this SOP runs the retrospective's scoped diff on one cluster at a time and reads its gate. It is not a watch: nothing here runs on a schedule, and the agent does not wait seven days inside a turn; the user asks again, or the retrospective's own Sunday run reads the cluster.

## Red Lines

- **Never run an upgrade.** No `gcloud container clusters upgrade`, `node-pools update`, `rolloutsequences create|update|upgrade|delete`, `rollouts pause|resume|cancel|force-complete-stage`, and no `clusters update`. Each is an operator step, quoted verbatim.
- **Never write a RolloutSequence or a label from here.** The stage configuration is text in the answer; a label is a pull request or an operator step.
- **Never move the collector's gate.** `clear`, `risk` and `pending` are the retrospective's, from its report; a gate you compute from a read of your own is not a gate.
- **Never let the rest follow on `pending`.** A gate that cannot be read holds the family as a risk does.
- **Never skip the precheck.** A plan that names a new sequence without the host project's listing in the answer is not a plan.
- **Never delegate to a Cluster Agent** or open kanban cards from this SOP; it is one fleet read, one readiness run and one scoped diff at a time.
