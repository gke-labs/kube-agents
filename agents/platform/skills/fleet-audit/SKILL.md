---
name: fleet-audit
description: Publish the findings of an autonomous fleet audit as one continuously-rewritten issue per audit stream on the GitOps repository's forge, and propose fixes as narrow remediation pull requests (merge requests on GitLab).
---

# fleet-audit — Audit Findings to a Ledger Issue

Every autonomous audit watchdog ends the same way: findings must reach a human somewhere durable,
reviewable, and de-duplicated. This skill is that ending, in two tiers:

- **Tier 1 — the ledger.** Each audit stream owns **exactly one open issue** on the forge, rewritten in
  full on every run and closed as completed when the fleet comes back clean. An operator watches one
  issue per stream instead of drowning in chat logs.
- **Tier 2 — the fixes.** When a finding's remediation is a file in this repository, it travels
  separately as a **narrow pull request carrying only that fix**, linked back to the ledger.

The split is the point. A report is not a change, so a report is not a pull request — and a fix is
not a report, so it carries a real diff a reviewer can read in one screen.

`/opt/data/profiles/platform/skills/fleet-audit/scripts/audit_report.py` owns every deterministic operation: credential
minting, label creation, issue creation and rewriting, branch handling, staging, committing,
pushing, pull-request creation, closing, the run-over-run delta, and every timestamp. **Your job is
to inspect the fleet read-only and emit a `findings.json`.** You never hand-write an issue body or a
PR body, never invent a timestamp, and never open an issue or pull request yourself — not with
`vcs.py issue create` or `vcs.py proposal create`, and not any other way —
that is precisely why every ledger looks the same and why the delta between runs is computable.

## Audit streams

Only these registered audit ids may own a ledger. Any other id is rejected before a single git or forge
operation runs. The issue title is `[audit] <human name> — <n> findings (<c> critical)` (singular
`1 finding` when there is exactly one), where the human name is the one `cron/jobs.json` gives that
watchdog — **not** a prettified form of the audit id:

| Audit id                      | Rendered ledger title                                                          |
| ----------------------------- | ------------------------------------------------------------------------------ |
| `compliance-audit`            | `[audit] Security & RBAC Posture Audit — 7 findings (2 critical)`              |
| `security-patch-orchestrator` | `[audit] Upgrade & Patch Readiness Audit — 7 findings (2 critical)`            |
| `obtainability-audit`         | `[audit] Workload Reliability Audit — 7 findings (2 critical)`                 |
| `fleet-wide-cost-analysis`    | `[audit] Fleet Waste Audit — 7 findings (2 critical)`                          |
| `fleet-consistency-drift`     | `[audit] Fleet Consistency Drift Audit — 7 findings (2 critical)`              |
| `ai-security-audit`           | `[audit] AI Workload Security Audit — 7 findings (2 critical)`                 |
| `stockout-prevention`         | `[audit] Fleet Stockout Prevention & Capacity Audit — 7 findings (2 critical)` |
| `gcp-networking-fabric-audit` | `[audit] GCP Networking Fabric & VPC IPAM Audit — 7 findings (2 critical)`     |
| `gce-compute-fleet-audit`     | `[audit] GCE Compute Engine and MIG Fleet Audit — 7 findings (2 critical)`     |

The mapping lives in `AUDITS` at the top of `audit_report.py` and mirrors `cron/jobs.json`; a test
fails if the two drift apart. Do not restate a title anywhere else.

## Run an audit

Which request you have decides what you do:

1. **Run one stream, per its SOP** (a kanban card or a delegated task naming the stream): you are
   the audit worker. Run the steps below yourself; do not reach for `hermes cron run`. If the
   audit cannot run in this session (no credentials, a broker that never answers, or a
   `START REFUSED`), call `kanban_block` with the reason; never `kanban_complete` a card as done
   when no findings and no report were produced (#1876).
2. **Trigger the scheduled cron job, or run several or all audits:** never run an audit inline.
   Open `references/on-demand.md` first.
3. **What an audit found:** run nothing. The
   [`fleet-audit-reports`](../fleet-audit-reports/SKILL.md) skill answers from the stored reports.

Run every script by its full path. A kanban worker starts in its card's workspace, where
`./skills/...` and `governance/...` name nothing.

1. **`start`, before anything else.**

   ```bash
   /opt/data/profiles/platform/skills/fleet-audit/scripts/audit_report.py start --audit <audit-id> [--repo "<owner>/<repo>"]
   ```

   It prints one JSON line: `sop`, the stream's SOP by full path; `checks`, the roster to run;
   `findings_path`, where your document goes; `workspace`, where remediation manifests go; `mode`
   (`content`, `directory` or `none`); `carried`, the findings the open ledger holds; and
   `next_steps`. A `START REFUSED` line means the stream is already running: say so and stop. A
   `BROKER UNAVAILABLE` line means the credential proxy did not answer: check the
   credential-proxy pod and re-run the same command. If your own `start` for this stream already
   succeeded in this session, do not run it again. Every field: `references/lifecycle.md`.

2. **Read the SOP at `sop` in full, then run its checks**, read-only, against every cluster in
   scope. Where the SOP runs a collector, run it the way the SOP says. Append each check's slug and
   the exact command you issued to that cluster's `checks_run` as the check completes. Content-mode
   reads (`grep`, `list`, `fetch`) and `draft`: `references/lifecycle.md`.
3. **Write the findings document at `findings_path`.** On a stream whose SOP runs a collector,
   start it from the manifest with
   `/opt/data/profiles/platform/skills/fleet-audit/scripts/audit_report.py draft --audit <audit-id> --manifest-file <manifest>`:
   it fills `scope` and one finding per candidate, and you write each finding's `recommendation`
   and `remediation`. Fields, validator rules, scope, `declared` and the evidence rules:
   `references/findings-document.md`. Write every `manifest` remediation file under `workspace`
   before `finish`.
4. **`finish`, once your `start` succeeded, `mode: none` included.** With no forge it is what writes
   the findings to the report store; a run that skips it keeps nothing and holds the stream's lease
   for two hours.

   ```bash
   /opt/data/profiles/platform/skills/fleet-audit/scripts/audit_report.py finish --audit <audit-id> --findings-file <findings_path> \
     [--repo "<owner>/<repo>"] [--manifest-file <path> | --no-collector-manifest "<why>"] \
     [--decline-fix <finding-id> "<why>" ...]
   ```

   Exit 0 published. Exit 2 (`FINDINGS REJECTED`) published nothing and kept the run yours: fix
   what the message names and run `finish` again, never `start`. A `BROKER UNAVAILABLE` exit 2 is
   different: what it already published stands, so re-run `finish` once the broker answers. Output
   fields, exit codes, partial coverage and no forge: `references/finish.md`.

5. **Report.** On a scheduled run with `silent_ok: true`, the final response is exactly
   `[SILENT]`. Otherwise report `issue_url` in full and every URL in `prs_opened` and
   `prs_still_open`; on `status: "NO_FORGE"` the report is `summary` verbatim. An on-demand run is
   never silent. Zero findings, `HELD` and `silent_ok`: `references/clean-run.md`.

## Reference files

Open each file when its step comes:
`skill_view(name="fleet-audit", file_path="references/<file>")`, or `read_file` at
`/opt/data/profiles/platform/skills/fleet-audit/references/<file>`. Do not open anything under
`scripts/` with `skill_view` or `read_file`: the scripts are run, not read. Most are far over the
100,000 characters one `skill_view` result can hold, and how to call each one is in `next_steps`,
the SOP and these references.

| File                   | Open it                                                                             |
| ---------------------- | ----------------------------------------------------------------------------------- |
| `on-demand.md`         | for a request to trigger a cron job or run several audits; for the in-flight guard  |
| `lifecycle.md`         | for what each `start` field means, the workspace modes, content-mode reads, `draft` |
| `findings-document.md` | before you write the findings document                                              |
| `finish.md`            | before `finish`, and to read its output, an exit 2, `partial` or `NO_FORGE`         |
| `clean-run.md`         | for a run with zero findings, a `HELD` ledger, or `silent_ok`                       |
| `remediation.md`       | for a `manifest` fix, a `/remediate` comment, or a direct ask to fix a finding      |
| `ledger.md`            | for what a finding's ledger state means, and the ledger's size limits               |

## Red lines

- **Read-only against clusters.** An audit inspects; it never mutates a cluster. Remediation is
  proposed as a file in a pull request or as a command for a human to run, never executed.
- **Never `git add .` or `git add -A`.** The harness stages only the distinct paths you named in
  `remediation.path`, through `git --literal-pathspecs`, and refuses glob metacharacters in a path
  outright. Do not run your own `git add`.
- **Every `remediation.path` stays inside the workspace, and the harness proves it twice.** The string
  must be repo-relative with no `..`, no glob metacharacter, and no leading `:` — and before the
  file is read or staged it is re-resolved against the `workspace` root, where no path component may
  be a symlink and the resolved path must sit under the resolved root. Do not create a symlink in
  the workspace and point a remediation through it: `manifests/vendor/x.yaml` is beyond reproach until
  `manifests/vendor` is a link to `/etc`, and then the contents of a file outside the repository are
  committed to a public pull request. Nothing is read from a path that fails either test. The
  finding degrades to `manual` with a note saying so, the run logs a `SECURITY:` line naming the
  path, and the report still publishes — but no pull request opens for that finding until the path
  is a real file inside the workspace.
- **Never work around a refusal from the credential proxy or the broker.** When it refuses a
  repository, route or verb, that is an answer: report it, say what an administrator would change
  (for a repository the install does not manage, add it to the managed list in
  `$GITOPS_STATE_CONFIGMAP`), and stop. A `BROKER UNAVAILABLE` line is not that: the proxy did not
  answer, so re-run the same command once it does (step 1, `references/finish.md`). Never copy a
  skill or script into a `*_override` directory, or edit or re-path it, to get past either.
- **Never open a second ledger issue for a stream.** Do not run `vcs.py issue create`. If the stream
  already has an open ledger, `finish` rewrites it in place; that is the whole point.
- **Never open a remediation pull request yourself**, and never for a non-`manifest` finding.
- **Never reopen a merged remediation pull request.** A persisting finding gets a comment and a
  ledger state, not a resurrection.
- **Never delete a remediation branch**, `remote-branch delete` included. The harness closes stale
  pull requests and leaves the branch: if the finding comes back, the fix is pushed there again.
- **Never force-push a protected branch.** `main`, `master`, and `production` are refused.
- **Never hand-write a body, title, commit message, or timestamp.** They are generated so that the
  diff between two runs is meaningful.
- **Write every `manifest` remediation file before calling `finish`**, under the `workspace`
  directory. For a finding the sweep would open, `finish` exits 2 listing each
  one whose file is missing, at the path it named, and each `manual` one the collector clears for
  the `major` sweep whose candidate carries a `declaration`, at the declared file. An edit to the object goes in that file; a new object, such
  as a PodDisruptionBudget, goes in a new file in its directory, never over it (in a Kustomize root,
  see the Kustomize rule below). Write them, set
  `kind: manifest` and `path`, and run `finish` again. A declared `no-pdb` finding is the exception:
  `finish` writes its PodDisruptionBudget from the collector's `pod_selector` if you did not, and a
  `--decline-fix` for it is ignored unless its reason carries the URL (`https://…/pull/<n>`) of the
  pull request already carrying it. That URL rule applies only to a budget `finish` writes itself; in
  a Kustomize root it writes none, so a decline that names the kustomization stands:
  leave it `manual`, and never name its declaration's file. One
  whose candidate carries `pod_selector_withheld` (its selector also reaches a DaemonSet's, Job's or
  CronJob's pods) is neither written nor refused: that budget would permit no evictions. A finding the SOP itself makes `manual`, or
  one a pull request already carries, stays `manual`: pass `--decline-fix <id> "<why>"` and the
  reason is published on its ledger row. Every `finish` refuses until each listed fix is written or
  declined, so re-running it unchanged only refuses again; a `--dry-run` does not refuse. A declined
  fix, like a missing file on a `finish` with no run record, degrades to `manual`, keeps its
  evidence and recommendation, and says so in the ledger. The report still publishes. Do not rely
  on this: a declined or degraded finding is a fix a human now has to apply by hand.
- **Never publish a `gcloud` fix against a field a Config Connector declaration holds.** Write the
  declaration's file as a `manifest` instead. Where `finish` finds a `ContainerCluster` or
  `ContainerNodePool` whose `spec` carries the field the command's flag writes, in the GitOps repo
  (its clone, or through the broker in content mode) or in a context repo `start` still has checked
  out, it degrades the finding to `manual`, naming the file above the fenced command — because the
  command would be reverted on the next reconcile. A content-mode `--dry-run` does not preview it.
- **Never write a `manifest` fix that adds a new file to a Kustomize root its kustomization does not
  list.** A remediation carries one file, so it cannot add the `resources:` entry. Make it `manual`
  with the file and the `resources:` line in `remediation.note`. What `finish` does with such a fix:
  `references/remediation.md`, "Kustomize roots".
- **Never report a cluster you could not read as clean.** Put it in `scope.skipped`, or name what
  did not run in that cluster's `limitations`. Both make the run `partial`, which is the mechanism
  that stops the harness from closing fixes and retiring the ledger on evidence it never gathered.
- **Never name a check in `checks_run` that you did not run, and never a command you did not issue.**
  It is the only claim in the document the harness has to take on trust, and padding it turns every
  protection above back off: the run stops being `partial`, the ledger closes, and a fleet nobody
  looked at publishes as clean. The commands are published verbatim, so a padded entry is not a
  private shortcut — it is a false statement in a public issue, with your run's name on it.
- **Never run the audit inline when asked to run or trigger the cron job, or when asked to run all audits.** Trigger the schedule or report that the trigger is unavailable; see
  [Running a stream on demand](references/on-demand.md#running-a-stream-on-demand). When delegated a task to execute an audit stream per its SOP, run the audit directly using the two-command lifecycle.
- **Never call `start`, `finish`, or `remediate` for a stream you dispatched.** The run owns its
  stream's lifecycle end to end and has already published by the time the call returns to you. A
  second `finish` reads a findings document the run's own `start` consumed, so it publishes whatever
  happens to be left in the shared scratch directory — which on 2026-08-04 meant a ledger closed as
  clean, then a second ledger opened and closed from eight-hour-old data. Report the run's
  `response`; that is your entire job once the dispatch returns.
- **Never restore or hand-edit a `findings.json` to get a command to pass.** Not from a `.bak`, not
  by editing a `check` value until validation accepts it, not by blanking the list to force a close.
  `/opt/data/scratch` is shared and unversioned, so a file you did not write this run is not your
  run's data. If `finish` rejects your document, fix the audit, not the file.
