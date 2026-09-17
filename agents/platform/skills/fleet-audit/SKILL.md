---
name: fleet-audit
description: Publish the findings of an autonomous fleet audit as one continuously-rewritten GitHub issue per audit stream, and propose fixes as narrow remediation pull requests.
---

# fleet-audit — Audit Findings to a Ledger Issue

Every autonomous audit watchdog ends the same way: findings must reach a human somewhere durable,
reviewable, and de-duplicated. This skill is that ending, in two tiers:

- **Tier 1 — the ledger.** Each audit stream owns **exactly one open GitHub issue**, rewritten in
  full on every run and closed as completed when the fleet comes back clean. An operator watches one
  issue per stream instead of drowning in chat logs.
- **Tier 2 — the fixes.** When a finding's remediation is a file in this repository, it travels
  separately as a **narrow pull request carrying only that fix**, linked back to the ledger.

The split is the point. A report is not a change, so a report is not a pull request — and a fix is
not a report, so it carries a real diff a reviewer can read in one screen.

`./skills/fleet-audit/scripts/audit_report.py` owns every deterministic operation: credential
minting, label creation, issue creation and rewriting, branch handling, staging, committing,
pushing, pull-request creation, closing, the run-over-run delta, and every timestamp. **Your job is
to inspect the fleet read-only and emit a `findings.json`.** You never hand-write an issue body or a
PR body, never invent a timestamp, and never call `gh issue create` or `gh pr create` yourself —
that is precisely why every ledger looks the same and why the delta between runs is computable.

## Audit streams

Only these registered audit ids may own a ledger. Any other id is rejected before a single git or gh
command runs. The issue title is `[audit] <human name> — <n> findings (<c> critical)` (singular
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

## Running a stream on demand

Each stream's cron job id **is** its audit id, so an operator asking for a run off-schedule is asking
for one command per stream:

```
HERMES_HOME=/opt/data/profiles/platform /opt/hermes/.venv/bin/hermes cron run compliance-audit
```

Every stream's cron job lives in this profile's own roster, ticked once a minute by the Chat Agent's
`profile-cron-tick`. `hermes cron run` marks the job due rather than running it here; the next tick
picks it up within a minute and runs it through the identical path the 06:20 tick uses, with the
stream's prompt verbatim, its `skills` preloaded, and this profile's `max_turns`.

`cronjob(action='run')` is not the route. Where the session cannot take a detached result — a
one-shot `hermes -z`, a stateless HTTP turn, a Kanban worker, a nested cron run — or where the
dispatch pool is full, it
executes the job synchronously inside the session that calls it, which is the re-enactment the next
paragraph exists to prevent. Elsewhere it hands the run to the background delegation executor and
returns a handle; that is closer to what you want, but `hermes cron run` is the one route that
behaves identically on every runtime and always runs in a fresh process.

**Your shell cannot reach that command, and there is no substitute yet.** It runs on the gateway pod,
where `hermes` and `/opt/data/profiles` are; your shell runs in the sandbox pod, which has neither, so
`command not found` there is the split working as designed rather than a broken install. When you hit
it, say the on-demand trigger is unavailable and that the stream will run on its 06:20 schedule. That
does not license either fallback: not `cronjob(action='run')`, and not running the audit yourself —
see the next paragraph. The gap is a deliberate deferral of the shell-sandbox design, not an
oversight.

**Do not run the audit yourself in the session that received the request.** A triggered run gets its
own process and its own turn budget. A session that improvises the audit instead has neither — and
when the request is "run them all", it has one turn budget for work the schedule spreads across
every stream and two days. That is not a hypothetical failure mode: on 2026-08-03 a single worker
asked to run all five streams that existed then issued zero `kubectl` commands, hand-typed five
empty findings documents, and published a fleet-wide all-clear.

The scheduler holds a per-job lock for the length of a run, so a stream already in flight is not
started a second time and cannot write its ledger issue twice. `cronjob(action='runs')` shows what
is running and what each attempt did.

**Each run reports on itself. Your own answer is a roll-up, not a copy.** Answer with one line per
stream — the stream, and that it is queued for the next tick. The reports arrive through each run's
own `deliver` setting; repeating them here sends the same content twice.

## The two-command lifecycle

Run both commands from your normal working directory — the profile directory, where `./skills/...`
resolves. **You are not in a git checkout, and you do not need to be.** The audit crons start in the
profile directory; the harness establishes its own workspace at
`/opt/data/gitops/<audit-id>/<owner>__<name>` and resolves every `remediation.path` against it. The
workspace is keyed by audit id because the audit streams share the volume with each other and with
every kanban worker: each one gets a tree nobody else writes in, so a colliding schedule can no
longer reset another stream's working copy out from under it. The repository comes from the
`$GITOPS_STATE_CONFIGMAP` ConfigMap, which the operator manages and which is readable before any
workspace exists.

What that workspace contains depends on the install, and `start` tells you which one you have as its
`mode` field:

- **`content`** — the workspace is an empty directory. The repository lives in the credential broker,
  which owns the only checkout; you write manifests into the directory and the harness hands the
  bytes over. Nothing on your side is a git repository, so there is nothing here to read the
  repository out of: use `list` and `fetch` (below) for that.
- **`directory`** — the workspace is a clone of the GitOps repository on the shared volume, and the
  harness runs `checkout`, `add`, `commit` and `push` inside it.

Everything else is identical, including where you write manifests and what `finish` publishes. Where
the two differ, this file says which mode it is talking about.

### Step 1 — `start`

Before inspecting anything, claim the workspace:

```bash
./skills/fleet-audit/scripts/audit_report.py start \
  --audit <audit-id> \
  [--repo "<owner>/<repo>"]
```

This resolves the target repository (using `--repo` if specified, falling back to the single
configured repo in `$GITOPS_STATE_CONFIGMAP`, or failing if ambiguous across multiple repos), mints
a repo-scoped GitHub token, establishes a clean workspace, ensures the audit's labels exist, locates
the stream's open ledger issue, and clears any findings document a crashed run left behind. If the
user asked for a specific repository that is not yet registered, instruct the user or cluster
administrator to add it to `$GITOPS_STATE_CONFIGMAP`. It creates **no branch** — there is no report
branch. It prints exactly one JSON line:

```json
{
  "issue": 128,
  "repo": "acme/fleet",
  "mode": "content",
  "workspace": "/opt/data/gitops/compliance-audit/acme__fleet",
  "findings_path": "/opt/data/scratch/findings_compliance-audit.json",
  "pending_remediation_requests": ["netpol-missing-payments"],
  "carried": [
    {
      "id": "cluster-admin-binding.prod-us-east._.clusterrolebinding-debug-binding",
      "check": "cluster-admin-binding",
      "cluster": "prod-us-east",
      "namespace": "",
      "object": "ClusterRoleBinding/debug-binding",
      "title": "ClusterRoleBinding debug-binding grants cluster-admin to a non-system subject"
    }
  ],
  "context_repos": ["acme/terraform-live"],
  "declared_intent_repos": ["acme/fleet", "acme/terraform-live"],
  "declared_intent_searched": [],
  "declared_intent_sources": [],
  "declared_intent_unsearched": [],
  "declarations_path": "/opt/data/scratch/declarations_compliance-audit.json",
  "sop": "governance/compliance_audit_sop.md",
  "checks": ["privileged-container", "host-namespace", "…"],
  "checks_contract": "Run every check above against every cluster you can read. …"
}
```

Write your findings to the `findings_path` it gives you. Do not pick your own path.

`checks` is your stream's full roster, handed over so coverage never depends on how far into the SOP
you read. **It is the work list, not a substitute for the SOP** — the slug says which check, the SOP
says what the check _is_ and what counts as a violation, so read the whole file before you start.
`sop` names it.

`workspace` is where your manifests go. **Every `remediation.path` is resolved against it**, so a
manifest written anywhere else is a file the harness will never find — the finding degrades to a
manual one and no pull request opens. `start` scrubs that directory before handing it to you;
`finish` does not, which is what lets the files you write in between survive.

`mode` is `content` or `directory`, and it changes one thing you can see: in `content` mode the
workspace is empty rather than a checkout. Read it rather than guessing from what is on disk.

`pending_remediation_requests` lists the findings a repository writer has already asked to be fixed,
parsed from the ledger's comments. **Write those manifests during inspection** — if the finding is
still reproducing at `finish`, its pull request opens immediately instead of a week later.

`carried` lists every finding the open ledger carries — its id, the check that found it, where it
is, and its title — read off the ledger body `finish` will compare your document against. **These
are the findings you are answering for.** For each one, this run ends one of four ways: you report
it again; you re-ran its check on that cluster, saw it gone, and say so under
[`resolved_because`](#the-findings-document) with the same `check`, `cluster`, `namespace` and `object`;
it is a posture now covered by a declaration and sits under `declared`; or you did not run that
check there and your `checks_run` does not claim you did. A run whose
`checks_run` says the check ran and whose document neither reports nor explains the finding is
**held** — see [The clean run](#the-clean-run). Empty when there is no open ledger or its body could
not be read (`start` says so on stderr).

`context_repos` names the repositories registered for **declared intent**: the `context_repos` key
of `$GITOPS_STATE_CONFIGMAP`, added by an administrator by hand, as `owner/name` slugs. A stream
whose SOP has a declared-intent step (today `obtainability-audit`, §4a) searches them before it
reports a posture as a finding. They are read and nothing else: the key is separate from
`managed_repos`, the harness never merges the two, so the broker's push gate, the repository
resolver and the sweep never see them. The list is empty when nothing is registered or the key
could not be read, which `start` says on stderr; the GitOps clone is searched either way. A private
context repository is readable through the broker's content-mode clone only (`inspect_repository.py
clone` and `open`): the broker mints a `contents: read` token for it per clone and installs it
nowhere. The sandbox's own CLI credential and the directory-mode clone cover managed repositories
alone.

`declared_intent_repos` is the set that step must account for: the GitOps repository plus every
`context_repos` slug, one entry each. `start` writes the same set to a run record beside the
findings document (`/opt/data/scratch/run_<audit-id>.json`), before it prints, and `finish` measures
the document's `declared_intent_searched` against that record rather than against the ConfigMap as
it stands at finish time. Every stream prints it; only a stream with a declared-intent step is held
to it.

`declared_intent_searched`, `declared_intent_sources` and `declarations_path` are the harness's own
half of that step, already done by the time `start` prints. On a stream with a declared-intent step,
`start` reads every repository in `declared_intent_repos` it can — each `context_repos` entry
through `inspect_repository.py clone` at the entry's `ref` when it has one, the GitOps repository
from the clone it just reset or through the same script in content mode — for `declares:`
frontmatter in OKF notes, within the paths each repository's `.kube-agents/intent.yaml` names
(`declared_intent_sources` lists each as `{repo, ref, paths}`, `paths` empty when the whole tree
was read). In content mode the copy is bounded the same way: `.kube-agents/` first, then only the
named paths, so the sibling script's file and byte caps count notes rather than manifests; a
repository with no usable intent file, or whose file names a path with nothing behind it, is copied
whole under those caps. `start` files what it found
at `declarations_path` and lists each repository it read completely — every note under the
searched paths arrived and was read; one the broker withheld or the harness could not decode costs
the repository its entry — as `owner/name@sha`. `finish` unions that list into the document's and
moves every finding a filed declaration covers to `declared` itself. A slug in `declared_intent_repos` missing
from `declared_intent_searched` is one the harness could not read; `declared_intent_unsearched`
lists each as `{repo, ref}`, stderr says why, and the SOP says what the worker does about it — its
own copy at that `ref`. On every other stream the three lists are empty and the file at
`declarations_path` holds none.

### Step 2 — Inspect the fleet (reasoning phase)

Enumerate the clusters in scope and inspect them **read-only** (`kubectl get/describe`,
`gcloud ... describe/list`). For every deviation you intend to report, capture the exact command you
ran and the output that proves it.

Keep a per-cluster tally as you go: for each check in the roster `start` printed, the slug and **the
exact command you issued for it**, appended the moment that check completes. `finish` requires it as
`checks_run` and rejects a slug with no command. Reconstructing the tally afterwards from memory is
how a check that never ran gets recorded as one that did — and now that each entry carries a command
that gets published, reconstructing it from memory is also how you end up publishing a command you
never issued.

If a remediation is a declarative file, write that file **under the `workspace` directory `start`
reported** and name its repo-relative path in the finding. The harness puts it on a branch of its
own.

**Directory mode only: do not leave unrelated uncommitted work in that tree during an audit.**
Opening a remediation pull request there requires switching branches, and the harness forces the
switch. It snapshots and restores every path you declared, and returns you to the branch you started
on — but a file it was never told about is not covered by that guarantee. Content mode switches no
branch and writes nothing back into the workspace, so this does not apply.

#### Reading the repository in content mode

The clone is gone, so three commands stand in for it. `grep` searches inside the files, `list` names
them, and `fetch` copies the ones you name into the workspace:

```bash
./skills/fleet-audit/scripts/audit_report.py grep  --audit <audit-id> --pattern 'namespace: payments'
./skills/fleet-audit/scripts/audit_report.py list  --audit <audit-id> --prefix clusters/prod-us-east
./skills/fleet-audit/scripts/audit_report.py fetch --audit <audit-id> --path clusters/prod-us-east/payments-netpol.yaml
```

`grep` runs broker-side and answers with matching lines; it is a fixed string unless `--regex`, and
`--prefix` narrows it. Use it when what you know is what a file says. `list` answers with paths and
sizes, never content — use it when what you know is where the file lives. The broker caps what each
returns, so read `truncated` on both and **pass `--prefix`** on a large repository. `fetch` writes
each file into the workspace at its repo-relative path, which is exactly where a remediation editing
that file has to end up; fetch it, edit it in place, and name the same path in the finding.

All three print `sha`, the commit of the tree the broker answered from. There is no `git` on this
side to ask, and the declared-intent record (`declared_intent_searched`, below) names each repository
as `owner/name@sha`; take the sha from the command whose answer you searched.

All three take `--branch`, and a second round needs it. Without it they answer from the base, so a
file the remediation branch has already changed — by an earlier run or by a reviewer — comes back as
the base has it, and committing the edit onto that branch reverts the change. The revert
fast-forwards, so nothing objects. Pass the remediation branch whenever the remote already has one;
a branch it does not have falls back to the base, which is what a first round wants anyway.

All three exit 2 in directory mode, where the clone already holds the file.

### Step 3 — `finish`

```bash
./skills/fleet-audit/scripts/audit_report.py finish \
  --audit <audit-id> \
  --findings-file <findings_path> \
  [--repo "<owner>/<repo>"]
```

The script validates the document, reconciles every finding against the pull requests already open
for this stream, rewrites (or opens) the ledger issue, comments the delta, opens pull requests for
the fixes that qualify, and closes the ones whose findings have stopped reproducing. It prints one
JSON line with twelve fields — `status`, `issue_url`, `new`, `resolved`, `prs_opened`, `prs_closed`,
`partial`, `coverage_gaps`, `silent_ok`, `declared`, the number of postures a repository
declaration kept off the ledger (it never decides silence), `postures_withheld`, the ids of the
posture findings `finish` held back because the document recorded no complete declared-intent
search (empty everywhere but on a declaring stream that skipped the step; see
[`declared_intent_searched`](#declared_intent_searched)), and `unaccounted`, the ids of the previous
findings a clean run was refused its close over (empty on every other outcome; see
[The clean run](#the-clean-run)):

- `{"status":"OPENED","issue_url":"…","new":7,"resolved":0,"prs_opened":["…"],"prs_closed":[],"partial":false,"coverage_gaps":[],"silent_ok":false,"declared":0,"postures_withheld":[],"unaccounted":[]}`
  — the stream had no open ledger.
- `{"status":"UPDATED","issue_url":"…","new":2,"resolved":3,"prs_opened":[],"prs_closed":["…"],"partial":false,"coverage_gaps":[],"silent_ok":false,"declared":0,"postures_withheld":[],"unaccounted":[]}`
  — the existing ledger was rewritten.
- `{"status":"CLEAN","issue_url":"…","new":0,"resolved":5,"prs_opened":[],"prs_closed":["…"],"partial":false,"coverage_gaps":[],"silent_ok":false,"declared":0,"postures_withheld":[],"unaccounted":[]}`
  — zero findings; the ledger closed as completed and its open fixes closed with it.
- `{"status":"HELD","issue_url":"…","new":0,"resolved":0,"prs_opened":[],"prs_closed":[],"partial":false,"coverage_gaps":[],"silent_ok":false,"declared":0,"postures_withheld":[],"unaccounted":["cluster-admin-binding.prod-us-east._.clusterrolebinding-debug-binding"]}`
  — zero findings, but the ledger was **not** closed: it carried findings whose checks this run's own
  `checks_run` says ran again, and the document neither reports nor explains them. Not a clean
  result; report it as [The clean run](#the-clean-run) says.

Add `--dry-run` to validate and print the rendered ledger body — and every PR body it _would_ open —
to stdout with **zero** git or gh side effects. It applies the same grouping and the same
degradation as the real run, so the branch names it names are the branch names it would create. It
resolves every `remediation.path` against the same `workspace` directory the real run uses, not against
the directory you happen to be standing in, so "the manifest is missing" is a finding of the dry run
and not a surprise at publish time. Use it whenever you are unsure your document is well formed.

Exit 0 means published. **Exit 2 means the run was rejected before publishing anything** — fix what
the message names and re-run; never delete the finding that tripped it. Three things reach exit 2:
the document failed a field rule, the file named by `--findings-file` is missing or is not valid
JSON, or `--audit` is not one of the registered ids above. Exit 1 is fatal and means something else
broke.

### Partial coverage

`partial` is `true` exactly when the run could not speak for the whole fleet: any entry in
`scope.skipped`, any cluster carrying a `limitations` note, any cluster whose `checks_run` is
short of the checks that _apply_ to it, or — on a stream with a declared-intent step — posture
checks that ran without a complete search record ([`declared_intent_searched`](#declared_intent_searched)).
`coverage_gaps` says which, and why — so `partial` is `true` if and only if `coverage_gaps` is
non-empty, and you can report from either.

A check the cluster's shape rules out is not a gap. Declaring it in that cluster's
`checks_not_applicable` (below) takes it out of the denominator, so a cluster that ran everything
that _can_ apply to it is a fully covered cluster. Without that, a fleet of Autopilot clusters is
permanently partial: the ledger never closes, `resolved` is pinned at `0`, and no stale remediation
pull request is ever cleaned up.

It does not mean "the description was truncated." A ledger too long for GitHub's body limit says so
in its own body and still carries true totals in its title; the audit saw everything, so nothing
about what the run may conclude changes. Coverage is the only thing `partial` tracks.

A gap changes what the run is _allowed to conclude_, because a finding's absence from an unread
cluster is not evidence that it was fixed. Over a partial run the harness:

- reports `resolved: 0` and posts no "resolved" delta, rather than announcing fixes it cannot see;
- closes **no** remediation pull request as stale, so a fix survives to the next complete run;
- does **not** close the ledger, even with zero findings — `status` is still `CLEAN`, but the issue
  stays open and gains a comment naming the gaps. The stream self-heals the day the fleet is fully
  readable again.

A partial run is never `[SILENT]` — `finish` returns `silent_ok: false` for it. Report the issue URL
and say which clusters were not covered. See [The clean run](#the-clean-run) for the full rule.

## The findings document

```json
{
  "audit": "compliance-audit",
  "scope": {
    "clusters": [
      {
        "name": "prod-us-east",
        "location": "us-east1",
        "project": "acme-prod",
        "checks_run": [
          {
            "check": "privileged-container",
            "command": "kubectl --context prod-us-east get pods -A -o jsonpath='{range .items[*]}{.metadata.namespace}{\"/\"}{.metadata.name}{\"\\t\"}{.spec.containers[*].securityContext.privileged}{\"\\n\"}{end}'"
          },
          {
            "check": "netpol-missing",
            "command": "kubectl --context prod-us-east get networkpolicy -A -o custom-columns=NS:.metadata.namespace --no-headers"
          },
          {
            "check": "workload-identity-off",
            "command": "gcloud container clusters describe prod-us-east --location us-east1 --project acme-prod --format='value(workloadIdentityConfig.workloadPool)'"
          }
        ]
      },
      {
        "name": "prod-autopilot",
        "location": "us-central1",
        "project": "acme-prod",
        "checks_run": [
          {
            "check": "netpol-missing",
            "command": "kubectl --context prod-autopilot get networkpolicy -A -o custom-columns=NS:.metadata.namespace --no-headers"
          },
          {
            "check": "workload-identity-off",
            "command": "gcloud container clusters describe prod-autopilot --location us-central1 --project acme-prod --format='value(workloadIdentityConfig.workloadPool)'"
          }
        ],
        "checks_not_applicable": [
          {
            "check": "legacy-metadata",
            "reason": "GKE Autopilot: no user-managed node pools to carry a metadata setting."
          },
          {
            "check": "hostpath-mount",
            "reason": "GKE Autopilot: hostPath volumes are rejected by the admission webhook."
          }
        ],
        "limitations": "RBAC denied `list clusterrolebindings`; check 2.4 did not run."
      }
    ],
    "skipped": [{ "cluster": "dr-west", "reason": "control plane unreachable" }]
  },
  "findings": [
    {
      "id": "netpol-missing-payments",
      "severity": "critical",
      "title": "payments namespace has no NetworkPolicy",
      "cluster": "prod-us-east",
      "namespace": "payments",
      "object": "Namespace/payments",
      "evidence": {
        "command": "kubectl --context prod-us-east get networkpolicy -n payments",
        "excerpt": "No resources found in payments namespace."
      },
      "impact": "All east-west traffic into the PCI namespace is unrestricted.",
      "recommendation": {
        "action": "Apply a namespace default-deny NetworkPolicy, then allow the two known callers.",
        "rationale": "Default-deny at the namespace is the smallest change that closes the exposure. A mesh AuthorizationPolicy would only cover injected pods, and payments runs two that are not.",
        "risk": "Unlabelled cross-namespace traffic breaks on apply. Run `kubectl -n payments get pods --show-labels` first to confirm the callers."
      },
      "remediation": {
        "kind": "manifest",
        "path": "clusters/prod-us-east/payments-netpol.yaml",
        "note": "Apply a default-deny NetworkPolicy."
      }
    }
  ],
  "declared": [
    {
      "check": "no-hpa",
      "cluster": "prod-us-east",
      "namespace": "payments",
      "object": "Deployment/api",
      "title": "api is pinned at three replicas by Terraform",
      "declaration": {
        "repo": "acme/terraform-live",
        "path": "clusters/prod-us-east/payments.tf",
        "excerpt": "replicas = 3  # fixed: the upstream rate limit is per-instance"
      }
    }
  ],
  "declared_intent_searched": [
    "acme/fleet@3f2a9c1d8e7b6a5f4c3d2e1f0a9b8c7d6e5f4a3b",
    "acme/terraform-live@8c7d6e5f4a3b2c1d0e9f8a7b6c5d4e3f2a1b0c9d"
  ]
}
```

(The `declared` entry and the `declared_intent_searched` list are illustrative and cross streams: a
real compliance document would be rejected for carrying either. `declared[].check` is validated
against the stream's `declarable` set in `AUDITS` — its posture checks, a subset of the roster — and
only `obtainability-audit` has one today, because only its SOP has a step that writes the list. A
non-empty `declared` or `declared_intent_searched` on any other stream exits 2; `[]` validates
everywhere.)

Field rules the validator enforces — a violation exits 2 naming the offending finding index and
field, and publishes nothing:

- `audit` must equal the `--audit` argument. An audit may only write to its own ledger.
- `scope.clusters` must be **non-empty**. An audit that enumerated nothing is a failure, not a clean
  run — if you could not list the fleet, say so loudly instead of reporting zero findings.
- `checks_run` is **required on every cluster** (the example above shows three entries per cluster
  for brevity; a real run carries one per check it ran). Each entry is an object with two required
  fields:
  - **`check`** — the backticked slug from the SOP heading that defines it (`netpol-missing`, not
    "2.6" and not prose). An unknown slug or a duplicate is rejected.
  - **`command`** — the literal invocation you issued on that cluster for that check, with its
    `--context`/`--project` and the namespace or resource it targeted. It must name one of
    `kubectl`, `gcloud`, `gsutil`, `bq`, `helm`, or `curl`; `echo`, `cat`, `python3 -c`, a call back
    into `audit_report.py`, and anything under eight characters are all rejected. One command per
    entry — the one that produced the evidence, not a summary of your approach.

  An empty list is rejected too, unless that cluster's `limitations` says why nothing ran.
  Enumerating a cluster and checking nothing on it is not a clean cluster — it is an audit that did
  not happen, and without this field the harness cannot tell the two apart. See
  [Scope, skipped, and limitations](#scope-skipped-and-limitations).

- `checks_not_applicable` is **optional**, and says which checks the cluster's shape rules out.
  Each entry is an object with two required fields:
  - **`check`** — the same slugs `checks_run` uses. An unknown slug, a duplicate, or a slug that
    also appears in this cluster's `checks_run` is rejected: a check either ran or could not.
  - **`reason`** — why the check _cannot_ apply here, naming the property of the cluster that rules
    it out ("GKE Autopilot: no user-managed node pools to carry a metadata setting"). Anything
    under sixteen characters is rejected, which is enough to stop "N/A" and "n/a — autopilot".

  These checks leave the coverage denominator instead of counting as missing, so a cluster that ran
  everything that _can_ apply to it is fully covered. That is the difference between a fleet whose
  ledger can close and one that is permanently partial. Use it only for a check the cluster's shape
  forbids — a check you could have run and did not is a `limitations` note and a real gap. Every
  entry is published in the ledger under _Not applicable_, with its reason, where a reviewer who
  knows the cluster can call an excuse for what it is.

- `resolved_because` is **optional**, and is how a run that found nothing says why a finding the
  ledger was carrying is gone. One entry per previous finding — take the identity from `start`'s
  `carried` list — carrying the same four identity fields a finding has and a `reason` of at least
  sixteen characters saying what the command showed:

  ```json
  "resolved_because": [
    {
      "check": "cluster-admin-binding",
      "cluster": "prod-us-east",
      "object": "ClusterRoleBinding/debug-binding",
      "reason": "kubectl get clusterrolebinding debug-binding returned NotFound; the binding was deleted on 2026-09-16."
    }
  ]
  ```

  `check` must be a slug in the SOP's roster, `cluster` must be in `scope.clusters`, `namespace` is
  omitted for a cluster-scoped object, and the entry's identity may not also be a finding in the
  same document. Each entry's id and `reason` are published in the run's closing (or held-open)
  comment, next to the evidence table, so a retired finding carries the sentence that retired it;
  nothing renders in the ledger body. The entry exists so `finish` can tell "fixed" from "not
  written down" before it closes one (see [The clean run](#the-clean-run)). Write it only for a
  finding you re-ran the check for and saw gone — it is a statement in a public issue, the same as
  a `checks_run` command. A posture that is now covered by a declaration needs no entry: list it
  under `declared` and it is accounted for.

- `check` is **required**, and is the backticked slug in the heading of the SOP check that produced
  the finding. Anything outside that SOP's roster is rejected.
- **Do not write an `id`.** The harness derives it as `<check>.<cluster>.<namespace>.<object>` — one
  grammar for all audit streams — lowercasing each part, replacing every run of non-alphanumerics
  with `-`, and substituting `_` for an absent namespace. Any `id` in the document is discarded.

  This used to be the model's job, specified in prose, and it was the wrong job to give it. A join
  key re-derived by inference is not a key: on 2026-08-03 one stream spelled the same nine findings
  three different ways in three consecutive runs, and because `compute_delta` joins on this string,
  the third run announced four unfixed criticals — three internet-reachable control planes among
  them — as **resolved**, on a ledger whose whole purpose is to say what is still broken. Derivation
  is what makes "the same problem keeps the same id" a property of the code rather than a request.

  What you still control is `check`, `cluster`, `namespace` and `object`, because identity is those
  four. Name the durable object the check judged — the owning controller, never the pod, whose name
  carries a random suffix — and never put a timestamp, counter, version, or run id in it. Two
  findings agreeing on all four are the same finding, and the document is refused rather than
  silently collapsed.

  The derived id still has to satisfy `^[a-z0-9]([a-z0-9._-]{0,98}[a-z0-9])?$` with no `..` run and
  no `.lock` suffix, and is shortened to fit: the id is the join key of the ledger's hidden delta
  block and of the `audit-persists:<id>` marker — both line-anchored regexes a space or a newline
  would break — and an operator types it by hand in `/remediate <id>`. An id that had to be
  shortened ends in `-<six hex characters>`, a digest of the id it was shortened from, because
  trimming alone lands two long objects in one long-named namespace on the same string and the
  duplicate-identity refusal above would then reject the whole document over two findings that are
  genuinely different. The digest is a function of that finding's four fields and nothing else, so
  it is the same next week.

- `severity` is one of `critical`, `major`, `minor`.
- `namespace` may be empty for cluster-scoped objects.
- `evidence.command` is **required and non-empty**.
- `recommendation` is **required on every finding**, with all three of `action`, `rationale`, and
  `risk` non-empty. See below.
- `remediation.kind` is `manifest`, `gcloud`, or `manual`. `path` is required for `manifest`
  (repo-relative, no `..`, no absolute paths, no glob metacharacters) and forbidden for the other
  two. For `gcloud`, put the exact command in `note` — it is rendered as a runnable block.
- `declared` is **optional**, and is not a list of findings. Each entry is a posture a check would
  have flagged that a linked repository declares on purpose — see
  [`declared`](#declared) below. It carries `check`, `cluster`, `namespace`, `object` and `title`
  under the same rules as a finding, plus a `declaration` object whose `repo` is an `owner/name`
  slug, whose `path` follows the remediation-path rules, and whose `excerpt` is the non-empty lines
  that pin the property. No `severity`, no `remediation`, no `id`. The document is rejected when an
  entry's four identity fields match a finding's — a posture is reported or declared, never both —
  when its `cluster` is not in `scope.clusters`, or when its `check` is not one of the stream's
  declarable posture checks (a declared fault is a declared bug, and a stream with no
  declared-intent step has none). A document without the key validates as before.
- **A `path` is discovered, never invented.** Editing an object means writing over its existing
  declaration. Creating one means writing beside a sibling already applied to the same cluster and
  namespace — search the repository for `namespace: <namespace>`, then **open the hits and confirm
  one declares an object you observed on the target cluster** before writing beside it. A `grep` for
  a name is kind-blind and matches label lines and shared prefixes, so a hit is not a declaration
  until you have read it. In directory mode that search is a `grep` over the clone; in content mode
  it is the `grep` subcommand, which the broker runs over its own checkout, and `fetch` to read the
  hits it named.
  **The parent directory must already exist in the repository**; if no sibling
  can be confirmed, or the hits straddle two directories you cannot tell apart, the finding is
  `kind: manual` with no path. The harness cannot check this for
  you: it validates the shape of a path, not whether anything reconciles it, and it will create
  missing parents and commit the file happily. A manifest in a directory the deploying tool does not
  apply merges clean, closes the finding for exactly one run, and changes nothing on the cluster —
  then returns next run as `pr-merged-persists`, where neither documented explanation fits.

### Scope, skipped, and limitations

**A cluster appears in exactly one scope list.** Ask one question:

> Could you read it? **Yes** → `scope.clusters`; name any check that did not run there in that
> cluster's `limitations`, and any check that _cannot_ run there in its `checks_not_applicable`.
> **No** → `scope.skipped`, with a reason.

Nothing goes in both, and nothing in `scope.skipped` may appear in a finding. The validator enforces
both halves. This matters because the alternative produces **false all-clears**: put an Autopilot
cluster in `scope.skipped` because one node-level check cannot apply there, and every real finding
on a cluster you did audit gets suppressed along with it.

`limitations` is optional, and non-empty when present. The rendered scope table grows a
`limitations` column only when at least one cluster carries one.

**`checks_run` is not optional, and it is what the scope table counts.** Every cluster carries the
list of checks that ran against it; the table renders it as `7/11`, marked `⚠` where it falls short,
on every run whether or not anything was missed — a column that only appears on bad days is a column
nobody reads on good ones. A cluster with declared inapplicable checks renders as `7/7 (4 n/a)`,
counted against what applies rather than against the full roster. A shortfall of what _does_ apply
is a coverage gap in its own right: it makes the run `partial` exactly as an unreadable cluster
does, is named in `coverage_gaps`, and so the ledger will not close on it. That is the point. A run
that skipped eight of eleven checks and found nothing has not found nothing; it has not looked, and
before this field existed it published as `CLEAN` and closed the ledger.

Which means the two ways to defeat all of this are to claim a check you did not run, or to park one
in `checks_not_applicable` that you simply did not get to. The harness runs as a subprocess of you;
it cannot see your tool calls, so it cannot verify either claim — an inflated `checks_run` converts
a partial audit straight back into a false all-clear, and a padded `checks_not_applicable` does the
same by shrinking the denominator until the shortfall disappears. Publication is what makes both
expensive and, more importantly, **falsifiable**: every command you name is published under _How
this run checked the fleet_, and every exclusion with its reason under _Not applicable_, where a
reviewer or the next run can re-run the one and contest the other. Record each entry as its check
completes and paste the command you actually issued. Never add entries in advance, never round the
list up to the roster because the SOP happens to define that many, never write a command you did not
run, and never write a `reason` that does not name a property of the cluster — a fabricated one is a
lie with your name on it in a public issue, which is a worse outcome for you than an honest `7/11`.

An honest shortfall costs you nothing. It marks the run `partial`, keeps the ledger open, and gets
picked up next run. That is the system working.

### `recommendation`

Three fields, all required, all load-bearing for the human who has to decide:

- **`action`** — what to do. Imperative, one or two sentences.
- **`rationale`** — why _this_ fix and not the obvious alternative. **Name the alternative you
  considered and why you rejected it.** A rationale that restates the action is not a rationale.
- **`risk`** — what breaks on apply, and the read-only check to run first.

### `declared`

A finding says the fleet is wrong; a declared posture says the fleet is what somebody meant. The
list exists because the audits judge live state against generic practice, and a platform team that
pinned a replica count in Terraform on purpose was getting the same `no-hpa` finding every morning
until someone suppressed it by hand. The SOP's declared-intent step (`obtainability_audit_sop.md`
§4a, the pilot) has two halves. The harness reads every repository in `declared_intent_repos` for
OKF notes whose frontmatter carries a `declares:` list — items of `{check, namespace, object}` plus
an optional `cluster`, `object` as `Kind/name` — within the paths each repository's
`.kube-agents/intent.yaml` names, and `finish` moves every finding one covers here itself: an exact
lookup on `(check, cluster, namespace, object)`, then on the fleet-wide `(check, namespace, object)`,
the finding's `cluster` and `title` kept and the note's `repo`, `path` and title as the declaration.
For `hpa-cannot-scale`, the one slug that names both a posture and a fault, the join moves only the
`min == max` shape, read off the severity the SOP fixes for it (`major`); a declaration matching the
`minor` dangling-target fault is reported on stderr and not applied.
The worker's half is the `provisioning/` pins HCL and YAML make in the GitOps clone, which have no
machine-readable form yet; a match there is moved here by the worker with the lines that pin the
property as `excerpt`. A posture a `declares:` note covers is written to `findings` like any other
and the join moves it; a candidate the worker leaves out because it found the note itself gives the
join nothing to move, and the declaration never reaches the ledger.

What the shape enforces:

- **It is not a finding.** No id is derived, so a declared posture never enters the hidden delta
  block: it is not announced as new when the declaration appears and not announced as resolved when
  it goes, and nothing about it is ever promoted to a pull request. A finding that moves here
  _does_ read as resolved in that run's delta — that is the intended outcome, and the ledger's
  _Declared intent_ section says where it went.
- **It is refutable.** Every entry names `repo:path` and quotes the lines that pin the property,
  and the ledger renders both, so a reviewer who disagrees changes or removes the declaration and
  the posture returns as a finding on the next run. A declaration the worker did not read is not
  one it may cite.
- **It justifies posture, never a fault.** Which checks may move here is the stream's `declarable`
  set in `AUDITS`, four for the pilot, and the validator rejects any other check with exit 2. A
  drain-blocking budget declared in a repository is a declared bug and stays a finding, and a
  document that lists it under `declared` publishes nothing.

### `declared_intent_searched`

A top-level list of `owner/name@sha` strings: each repository the declared-intent step searched,
at the commit it was read at. It is the record that the step ran, and it is what makes a skipped
step visible. Nothing else in the document can: a run that skipped the search and published every
posture as a finding, and a run that skipped it and left a candidate out, both validated as complete
before this field existed.

What `finish` does with it:

- **Complete means every repository `start` named.** The list, sha stripped and case-folded, must
  cover every slug in `start`'s `declared_intent_repos` — the GitOps repository and every
  `context_repos` entry — measured against the run record `start` wrote, not the ConfigMap at finish
  time. Extra repositories are allowed. The sha is checked for shape only (7 to 40 lowercase hex
  characters), so the record is as forgeable as a padded `checks_run` and carries less; it makes a
  skipped step visible, not impossible.
- **The harness's own search counts first.** `start` records each repository it read completely
  in the run record, and `finish` unions that list into the document's before it measures, so a
  repository the harness read needs no entry from the worker and a document with no key at all
  is complete when the harness read every repository. What the worker owes is the rest: the
  repositories `start` listed under `declared_intent_repos` and not under
  `declared_intent_searched`.
- **It is owed whenever a declarable check ran.** Keyed on `checks_run`, not on the postures in
  `findings`, for the reason above: a candidate left out without a search reads exactly like one a
  declaration covered. A run on which none of the four checks ran anywhere owes nothing.
- **Anything less is no search, and the postures are withheld.** A union of the worker's list and
  `start`'s that misses a repository, or no run record: `finish` — real and `--dry-run` — takes every finding whose check is
  declarable out of the document, the dangling-target `hpa-cannot-scale` fault included because it
  shares its slug with the `min == max` posture, and adds one `coverage_gaps` sentence naming each
  withheld entry and the repositories not searched. The faults publish; `declared[]` entries publish.
  `partial` stays `bool(coverage_gaps)`, so the ledger does not close, `resolved` is `0`, no stale
  pull request is retired, and the withheld ids enter no delta block and no remediation pull
  request. The ledger names the withheld postures under _Declared intent not searched_ below the
  Scope table, the clean comment lists them, and the JSON line carries their ids as
  `postures_withheld`.
- **A complete record renders.** One line under Scope, `Declared-intent search: owner/name@sha, …`,
  so a reader can see what was read.

Where the sha comes from is the SOP's §4a, and it is the same for the harness and the worker: in
content mode `list`, `grep` and `fetch` print it for the GitOps repository and
`inspect_repository.py clone` and `open` print it for a context copy; in directory mode it is
`git -C <dir> rev-parse HEAD` on the GitOps clone and on the `workspace` that `clone` named for
the context copy.

The withhold binds the direct-ask path too: `remediate --finding <id>` applies it against the same
record and refuses a withheld id by name, because a pull request for a posture the ledger says was
held back would contradict the ledger. It applies the harness's declarations the same way, and
refuses an id a declaration covers for the same reason. A `/remediate` comment naming a withheld
posture is deferred, not refused, and one naming a declared posture is refused with the declaring
file named — see the answers list under [Remediation pull requests](#remediation-pull-requests).

## Evidence rules

**A finding with no reproducible command is dropped, not softened.** If you cannot produce the exact
read-only command that a reviewer can paste into a terminal to see the same thing you saw, the
finding does not go in the file. Do not downgrade it to `minor`, do not hedge the title, do not write
"appears to". Omit it.

Corollaries:

- `evidence.excerpt` is the real output, copied. Never paraphrase it and never synthesise
  plausible-looking output. The harness trims long excerpts and long commands for you.
- **Never paste a Secret's `data:`, a token, a password, or a private key into an excerpt.** Report
  that the Secret exists and what is wrong with it; the command in `evidence.command` is how a
  reviewer sees the rest, under their own credentials.

  The harness redacts high-confidence credential shapes as a backstop — a `data:`/`stringData:`
  block, an environment variable whose name ends in a credential word, a field named like a secret
  carrying a value on the same line, a self-identifying token prefix, a PEM header, an
  `Authorization:` value — replacing them with `[redacted by audit_report.py]`. It is deliberately
  conservative and **does not** touch bare base64, a boolean, or an absolute path, because
  legitimate audit output is full of all three. Treat the backstop as a seatbelt, not a licence: it
  will not catch a credential that looks like ordinary output.

- Report what the command showed, not what you infer it implies. Inference belongs in `impact`.
- One finding per object. Do not roll up "12 namespaces lack NetworkPolicies" into one finding — each
  gets its own stable id so each can resolve independently.

## What the ledger says about each finding

Every finding renders in exactly one state, computed fresh each run from whether it still reproduces
and what pull request sits on its branch. Nothing is stored between runs.

| State                | Rendered as                           | Meaning                                    | What the harness does                                        |
| -------------------- | ------------------------------------- | ------------------------------------------ | ------------------------------------------------------------ |
| `open`               | `open`                                | Reproduces; no pull request                | Nothing, unless it qualifies for auto-promotion              |
| `pr-open`            | `fix proposed`                        | Reproduces; a fix is open on its branch    | **Labels re-asserted.** The pull request itself is untouched |
| `pr-merged-persists` | `⚠ fix merged, still reproduces`      | Reproduces; the fix **merged anyway**      | Comments once on the merged PR; never reopens it             |
| `refused`            | `fix refused`                         | Reproduces; a **human closed** the fix     | Nothing. The close stands until someone says `/remediate`    |
| `withdrawn`          | `fix withdrawn, awaiting re-proposal` | Reproduces; the **harness closed** the fix | Treats it as having no pull request — it is promotable again |

Every row above says "reproduces", and that is not an accident: **a finding that stopped reproducing
is not in the document at all**, so it has no row in the ledger to carry a state. Two further states
exist in the code — `resolved` and `resolved-merged` — but neither is ever rendered here. A
resolution is announced in the delta comment, by id and title recovered from the previous body, and
the finding's open pull request is closed as stale. A resolution whose fix had already **merged** is
the ordinary, expected ending, so nothing extra is closed and nothing extra is said.

Three of the five are easy to misread:

- **`pr-open` is not refreshed.** An open pull request's diff is left exactly as it is, because a
  reviewer may have pushed onto it and a nightly force-push would silently discard their work. The
  ledger links it; the diff is whatever a human last made it. Its **labels** are the single
  exception, re-asserted on every run that finds it still open: they are the harness's own index of
  what it still owns rather than anything a reviewer authored, and a stripped `agent:audit` or a
  `severity:` frozen at what the group used to be loses the pull request from the views triage works
  from. Labels only — no push, no rewritten body, so the promise above still holds.
- **`refused` is a human decision, not a rejected command.** It means someone closed the remediation
  pull request without merging it. That is a considered "no", and the harness never overrules it by
  re-opening the same fix tomorrow morning.
- **`withdrawn` is the other half of that.** A closed unmerged pull request is two different events,
  and the discriminator is the `audit:stale-closed` label the harness applies when it closes one as
  stale. Its finding is promotable again on the usual terms; a `refused` one is not. Do not strip
  that label — without it the close reads as a human rejection and the finding is never re-proposed.

The escape hatch for a `refused` finding is `/remediate <id>` from someone with write access, and it
must be written **after** the close. An older command in the thread is reported as _superseded_
rather than honoured: comments are never edited away, so a March request would otherwise re-open an
April close every morning forever. Post a fresh one.

`pr-merged-persists` is the state worth reading twice: a fix merged and the deviation is still
there. Either the remediation was incomplete or something outside this repository reverted it.

Below the findings, a ledger whose document carried a `declared` list renders a **Declared intent**
table: one row per entry, with the check, the cluster, the object, `repo:path`, and the title and
excerpt. Rows have no state and no id — nothing in that table is tracked between runs — and the
table is capped at 50 rows and says how many it left out. On a clean run the ledger closes without
being rewritten, so the all-clear comment lists the declared postures instead, with the same
`repo:path` pointers — and that comment is the last ledger record: a later clean run with nothing
but declarations opens no ledger and posts nothing, because the ledger tracks findings and a
standing declaration is the same every morning. From then on the declaration in the repository is
the record, and `finish` reports the count as `declared`.

## Remediation pull requests

A pull request is opened for a finding only when its remediation is a `manifest` — there is nothing
to put in a diff otherwise. Three paths lead there:

- **Auto-promotion.** A finding that is `critical`, is a `manifest`, and has no live pull request on
  its branch is promoted automatically by `finish` — **at most five per run**. The surplus is named
  in the ledger as awaiting `/remediate`, so nothing is silently dropped. "Live" excludes a pull
  request the harness itself closed as stale (that one is re-openable) and includes one a human
  closed or merged (those are not).
- **`/remediate <finding-id>`**, or `/remediate all`, commented on the ledger by someone with write
  access to the repository. This path is uncapped: a human asked for that one by name.
- **A direct ask.** A collaborator asking the agent, in the agent's own task, to fix a named
  finding; the agent answers with the `remediate` subcommand. Uncapped for the same reason as
  `/remediate` — and distinct from a comment read on the ledger, which is the harness's to answer
  (`start` reports the ones that passed its gates as `pending_remediation_requests`). Fresh pull
  requests only: a finding whose pull request a human closed is reported as `superseded`, never
  re-proposed — a direct ask carries no GitHub identity, so the after-the-close escape hatch
  above stays with the write-gated comment.

Every `/remediate` gets exactly one answer, and the answer is never silence:

- Accepted — one acknowledgement comment on the ledger naming each target and **what happened to
  it**, never a count: the pull request URL, or "already open" with its labels re-asserted and its
  diff untouched, or _superseded_ by a human close written after the request, or that publishing
  failed and the next run will retry. "3 requests processed" is indistinguishable from "3 requests
  silently dropped".
- Refused — one reply saying why, for a commenter without write access, a `/remediate` naming a
  finding that is not in the current document, one naming a non-`manifest` finding, or one naming
  a posture a repository declaration covers, the model's entry or the harness's: the reply names
  the declaring `repo:path`, on the findings branch and the clean branch alike, since neither
  "typo" nor "no longer reproduces" is true of it. Removing the declaration brings the finding
  back, and a new request then opens it.
- **Deferred**, when the target is a posture this run withheld for want of a declared-intent search
  ([`declared_intent_searched`](#declared_intent_searched)) — on the findings branch and on the clean
  branch alike, since "no longer reproduces" would be false there. One reply says the request is on
  hold and why, under its own `audit-deferred` marker, which nothing reads as an answer: the same
  comment is acted on, and acknowledged, by the first run that records the search and still sees
  the posture. The refused marker is never written for it, so the requester is not told their id was
  a typo and does not have to ask again.
- Refused **on syntax**, likewise once, because a command the parser will not honour is a person
  waiting for a fix that is never coming. `/remediate` is only read at the start of its own line outside
  block quotes, so one written mid-sentence or rendered inside a block quote / lazy continuation gets a reply
  pointing that out; one written with no target at all gets a reply too, because reading it as `all` would open
  every promotable pull request the cap allows on someone who typed the command and then went to look up the id.
  These replies carry the correct syntax and the promotable ids — up to ten, then "and N more", since a refusal
  is help and not a second copy of the report.
- Overtaken by a **clean run** — answered anyway, and answered _before_ the ledger closes. A run that
  finds nothing still replies to every unanswered `/remediate` in the thread to say the finding no
  longer reproduces, and whether the ledger is closing or staying open on partial coverage. This is
  the one morning the issue disappears, taking with it the thread the requester would have re-asked
  on, so it is the one morning silence is least affordable. Authorization is not consulted here:
  nothing is being acted on for anybody, and "it no longer reproduces" is equally true and equally
  useful to a commenter without write access.

Two deliberate silences. A mid-sentence or quoted `/remediate` from someone _without_ write access gets
nothing: their correctly-typed command would have been refused anyway, and two replies to one
comment that was probably never a command is a bot picking an argument. And a `/remediate` inside a
code span is prose about the command, not an attempt at it — which is why every `/remediate` the
harness itself writes into a comment is backticked. Keep it that way when you quote one: an
unbackticked command in a harness-authored comment is read back on the next run, and a bot that
answers itself never stops.

All of these are guarded by a hidden marker carrying the triggering comment's node id, so a standing
`/remediate` in the thread is answered once rather than every morning forever.

A `/remediate` read on the ledger is never run through the subcommand — `finish` answers those on
the comment's own timestamp, which is what lets a fresh post-close command revive a finding the
subcommand would report as `superseded`. Run a direct ask's targets through it, `--finding` once
per id:

```bash
./skills/fleet-audit/scripts/audit_report.py remediate --audit <audit-id> \
  --findings-file <findings_path> --finding <id> [--finding <id> …] [--issue <n>] \
  [--repo "<owner>/<repo>"]
```

**It opens exactly what you name, and nothing else.** The auto-promotion sweep does not ride along:
one `--finding` produces one pull request (or one, shared, for the group that path belongs to), never
five more for critical findings the requester never mentioned and cannot tell apart from the one they
did. Auto-promotion happens in `finish`, where the whole fleet is being reported on anyway.

It prints one JSON line — `status`, `prs_opened`, `already_open`, `superseded`, and `refused`:

- `{"status":"REMEDIATED","prs_opened":["…"],"already_open":["cluster-old"],"superseded":[],"refused":["ns-quota"]}`

`superseded` names targets whose pull request a human closed: that close stands, and the
subcommand does not re-propose them. Revival belongs to the write-gated `/remediate` comment
(§ above, honoured by `finish` on the comment's own timestamp) — or to `--override-human-close`,
which exists for the person at the terminal who could have written that comment themselves. An
agent relaying an ask it cannot tie to a GitHub identity never passes that flag.

`refused` names the targets whose remediation is not a readable file inside the workspace — either the
audit promised a manifest and never wrote it, or the path does not resolve inside the repository at
all. Both leave nothing to put in a diff; a `SECURITY:` line in the log says which one happened. The
other targets still open — `/remediate all` expands to every **manifest-remediation** id in the
document, and failing the batch over one unwritten file would answer a request for many fixes with
none. Say which were refused when you acknowledge the command.

Exit 2 means nothing was published, for one of three reasons — read the message before reporting
which: a named id is not in the document at all, a named target is not a `manifest`, or _every_
named target was refused because its file is not readable inside the workspace. The first two are fixed
by dropping the bad id and asking again; only the third is about writing manifests.

**Findings whose remediation paths intersect share one pull request.** They have to: separate
branches touching the same file conflict on merge. Promoting any member promotes the whole group —
the pull request names every member. That is why several findings can point at one shared manifest
and still produce one clean diff.

The group's branch is `platform-agent/fix-<audit-id>-<slug>-<digest>`, where the digest is over the
group's **sorted path set** and the slug is a readable fragment of the first path. It is keyed on
the files, not on a finding id, and that is load-bearing: ids are regenerated every run, so a branch
named after one of them gets renamed the day that finding resolves — orphaning the open pull request
and opening a duplicate against the same file.

The branch name is the only join key. There is no state file: `finish` reconstructs the entire
finding-to-pull-request mapping from one `gh pr list` call.

## Size

GitHub caps an issue body at 65,536 characters, and a pull request body at the same. The harness
targets 60,000 and will truncate the ledger's findings section to stay under it. Three consequences
you must not work around:

- **Findings are rendered severity-first**, so truncation only ever eats the least-severe end.
  Criticals are structurally safe.
- **The title's counts stay true.** If the body omits findings it says so explicitly. Never
  hand-trim your document to make it fit — the counts are how a reader learns the real total.
- **Truncation does not make a run `partial`** — see [Partial coverage](#partial-coverage), which
  owns that rule.

Every free-text field is clipped on the way out — title 300 characters, impact and each
`recommendation` sub-field 1,500, `remediation.note` 2,000, `evidence.command` 2,000,
`evidence.excerpt` **40 lines and** 2,000 characters, whichever it hits first, and
`cluster` / `namespace` / `object` 320. That last group is not a style rule. The renderer always
emits the **first** finding whatever it costs, so before those three were clipped one oversized
identifier on one finding could overflow the whole body and publish nothing at all, every morning,
until that finding stopped reproducing.

Resolution accounting is unaffected by truncation, because the two halves of the delta are measured
against different sets: **new** is judged against what the body rendered, and **resolved** against
every finding in the document, rendered or not. A finding cut for space still reproduces and is
never reported as fixed.

The **Declared intent** table is measured with the fixed sections, before the findings claim what
is left, so on a body near the limit it is the findings that yield — and it is row-capped, so what
it can cost is bounded.

The ledger's last section, **How this run checked the fleet**, is a collapsed table of every
`checks_run` entry — cluster, check, command. It is rendered last, against whatever budget the
findings left, and is dropped whole rather than half if it does not fit: a partial evidence table
reads as a short one, and "this run ran three checks" is a worse lie than saying nothing. You do not
write this section; you supply the commands and the harness publishes them.

## The clean run

If the audit finds nothing, still call `finish` with `"findings": []` and a populated
`scope.clusters`. With complete coverage the harness answers any `/remediate` still standing
unanswered in the thread, comments the date, the clusters covered and the same _How this run
checked the fleet_ table the ledger body carries, closes the ledger issue **as completed**, and
closes every remediation pull request still open for the stream. The answers come first,
deliberately: a reply posted after the close would land on an issue nobody is watching. The table
is there because a clean run does not rewrite the body: the comment is the only place the commands
behind an all-clear are published, and an all-clear nobody can re-run is one nobody can audit
after the fact.

**Zero findings plus a coverage gap is not a clean run, and it is not silent even on a stream with
no ledger.** With gaps, the harness opens one — titled `coverage incomplete (n gaps, 0 findings)`
rather than the all-clear phrasing — so the run leaves a durable artifact saying what it could not
see. Without this, a stream that inspected nothing produced no issue, no comment, and nothing to
notice: four streams did exactly that on 2026-08-03, and the only reason it surfaced is that a fifth
happened to have a ledger open from the day before.

**Zero findings over a finding the run checked again is not a clean run either.** Before it
closes, `finish` reads the previous body — the same findings `start` handed you as `carried`. For
every finding it carried, if this run's `checks_run` says the check that found it ran on that
cluster — the SOP's fleet-wide `kubectl get clusterrolebindings -o json | jq …` counts; it lists
every binding, `debug-binding` included — and the document neither reports the finding again,
carries a `resolved_because` entry for it, nor lists it under `declared`, the run either saw it gone
or left it out, and from the document the two are the same absence. The ledger stays open and gets
a comment naming each such finding and the check that ran (plus any `resolved_because` reasons and
declared postures the document does carry), no remediation pull request is closed, and `finish` returns
`status: "HELD"` with `resolved: 0`, `silent_ok: false` and the ids in `unaccounted`. Report it as
you would a partial run — the ledger URL and the held ids — and on the next run either report the
finding or, if you re-ran its check and saw the object gone, say so in `resolved_because`. On
2026-09-16 a compliance run closed its ledger as clean over a live cluster-admin binding its own
`checks_run` claimed to have checked; this is the guard that turns that close into a held ledger. A
check declared `checks_not_applicable` on that cluster did not run there and holds nothing — the
excuse is published in the evidence table, where a reviewer can weigh it.

A clean run is usually not news, and the closed issue is the record — but "clean" alone does not
decide it. **`finish` decides it, and returns the answer as `silent_ok`.** Read the flag; do not
reassemble it from `status`, `new`, `resolved`, and `partial` yourself. That arithmetic has more
clauses than it looks like it has, and re-deriving it is how a run talks itself into silence it has
not earned — on 2026-08-03 a run with two partially-covered clusters answered `[SILENT]` and its
ledger URL never reached the operator who had asked for it.

> **`silent_ok` is `true` only when the run moved nothing an operator needs to hear about:** nothing
> new, nothing resolved, no coverage gap, no held close, and no remediation pull request opened or
> closed.

Two rules follow, and they are the whole rule:

- On a **scheduled** run, `silent_ok: true` → the final response is exactly `[SILENT]`. Otherwise
  report, and every report carries `issue_url` in full.
- **An on-demand run is never silent.** `silent_ok` is the _scheduled_ verdict — it answers "would a
  channel want this?", and it cannot know a person asked. If someone dispatched this job, from a
  kanban card or straight from chat, they are waiting on the answer and
  `[SILENT]` throws it away. Report the outcome and the ledger URL whatever the flag says.

Three zero-finding runs come back `silent_ok: false`, and all of them matter:

- **`resolved > 0`** — the fleet was carrying findings yesterday and is not today. Something got
  fixed, and that is the best thing this audit ever gets to say. Reporting `partial` failures while
  swallowing this one would leave the operator hearing only bad news.
- **`partial: true`** — the ledger stayed open because the fleet was not fully read. "I found
  nothing" and "I could not look" must not arrive as the same silence.
- **`status: "HELD"`** — the ledger stayed open because the run did not account for findings it was
  carrying. "I found nothing" and "I did not write it down" must not arrive as the same silence
  either.

There is one case where the harness reports `new: 0, resolved: 0` without knowing it: if the
previous ledger body could not be read, the delta is unknowable, so it announces nothing rather than
declaring every live finding new. `silent_ok` follows the counts it can defend and comes back `true`
on an otherwise quiet run. The run logs
`Previous ledger body was unreadable; skipping the delta comment` to stderr and the ledger is still
rewritten correctly — the issue carries the truth either way.

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
- **Never open a second ledger issue for a stream.** Do not call `gh issue create`. If the stream
  already has an open ledger, `finish` rewrites it in place; that is the whole point.
- **Never open a remediation pull request yourself**, and never for a non-`manifest` finding.
- **Never reopen a merged remediation pull request.** A persisting finding gets a comment and a
  ledger state, not a resurrection.
- **Never delete a remediation branch.** The harness closes stale pull requests and leaves the
  branch: if the finding comes back, the fix is pushed there again.
- **Never force-push a protected branch.** `main`, `master`, and `production` are refused.
- **Never hand-write a body, title, commit message, or timestamp.** They are generated so that the
  diff between two runs is meaningful.
- **Write every `manifest` remediation file before calling `finish`**, under the `workspace`
  directory. A path that is not on disk does not fail the run — that one finding degrades to
  `manual`, keeps its evidence and recommendation, and says in the ledger that the fix was named but
  not written. The report still publishes. Do not rely on this: a degraded finding is a fix a human
  now has to apply by hand.
- **Never report a cluster you could not read as clean.** Put it in `scope.skipped`, or name what
  did not run in that cluster's `limitations`. Both make the run `partial`, which is the mechanism
  that stops the harness from closing fixes and retiring the ledger on evidence it never gathered.
- **Never name a check in `checks_run` that you did not run, and never a command you did not issue.**
  It is the only claim in the document the harness has to take on trust, and padding it turns every
  protection above back off: the run stops being `partial`, the ledger closes, and a fleet nobody
  looked at publishes as clean. The commands are published verbatim, so a padded entry is not a
  private shortcut — it is a false statement in a public issue, with your run's name on it.
- **Never run the audit inline when asked to run the cron job.** Dispatch it; see
  [Running a stream on demand](#running-a-stream-on-demand).
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
