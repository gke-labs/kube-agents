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

Only these five audit ids may own a ledger. Any other id is rejected before a single git or gh
command runs. The issue title is `[audit] <human name> — <n> findings (<c> critical)` (singular
`1 finding` when there is exactly one), where the human name is the one `cron/jobs.json` gives that
watchdog — **not** a prettified form of the audit id:

| Audit id                      | Rendered ledger title                                               |
| ----------------------------- | ------------------------------------------------------------------- |
| `compliance-audit`            | `[audit] Security & RBAC Posture Audit — 7 findings (2 critical)`   |
| `security-patch-orchestrator` | `[audit] Upgrade & Patch Readiness Audit — 7 findings (2 critical)` |
| `obtainability-audit`         | `[audit] Workload Reliability Audit — 7 findings (2 critical)`      |
| `fleet-wide-cost-analysis`    | `[audit] Fleet Waste Audit — 7 findings (2 critical)`               |
| `fleet-consistency-drift`     | `[audit] Fleet Consistency Drift Audit — 7 findings (2 critical)`   |

The mapping lives in `AUDITS` at the top of `audit_report.py` and mirrors `cron/jobs.json`; a test
fails if the two drift apart. Do not restate a title anywhere else.

## Running a stream on demand

Each stream's cron job id **is** its audit id, so an operator asking for a run off-schedule is asking
for one tool call:

```
cronjob(action='run', job_id='compliance-audit')
```

**Dispatch it. Do not run the audit yourself in the session that received the request.** A
dispatched job runs the same path the 06:20 tick runs: its own session, its own prompt naming the
SOP and the line range its checks live in, its own skills, model, and turn budget. A session that
improvises the audit instead has none of that — and when the request is "run all five", it has one
turn budget for work the schedule spreads across five runs and two days. That is not a hypothetical
failure mode: on 2026-08-03 a single worker asked to run all five streams issued zero `kubectl`
commands, hand-typed five empty findings documents, and published a fleet-wide all-clear.

One call per job, and the call is synchronous — it returns after that stream's run finishes.
Dispatch the next one once it comes back rather than firing five and assuming. If `executed` is
`false` nothing ran: the scheduler already owns the fire, or the job is paused, and the response
says which.

**The run reports back to you. Relay what it says; do not reconstruct it.** A completed dispatch
returns the run's own closing report in `response` — the ledger issue URL, the finding counts, any
coverage gap it hit — alongside `execution_success`, the `output_file` holding the run's full
transcript, and a `delivery_error` if the chat delivery leg failed (which says nothing about whether
the audit itself succeeded). The run has already published its ledger by the time you read this. You
have nothing left to publish, and the scratch directory it worked in is shared and already consumed;
anything you rebuild from it belongs to some other run. If `response` is empty, say the run returned
no report and point at `output_file` — that is a real, reportable failure, not an invitation to run
the audit yourself. A `response` of `[SILENT]` is the same situation wearing a different hat: the
run suppressed its own delivery, which the SOPs forbid on an on-demand run, so read the
`output_file` it named and report from that.

Your own kanban card is yours to close. The dispatched run cannot close it for you and will be told
so if it tries.

## The two-command lifecycle

Run both commands from your normal working directory — the profile directory, where `./skills/...`
resolves. **You are not in a git checkout, and you do not need to be.** The
audit crons start in the profile directory; the harness clones the GitOps repository itself, into
`/opt/data/gitops/<audit-id>/<owner>__<name>` on the shared volume, and runs every git and gh call
inside it. The clone is keyed by audit id because the five streams share the volume with each other
and with every kanban worker: each one gets a tree nobody else writes in, so a colliding schedule
can no longer reset another stream's working copy out from under it. The repository comes from the
`Git Repo:` line of `/opt/data/SETTINGS.md`, which the operator writes at provisioning time and
which is readable before any clone exists.

### Step 1 — `start`

Before inspecting anything, claim the workspace:

```bash
./skills/fleet-audit/scripts/audit_report.py start --audit <audit-id>
```

This resolves the target repository, mints a repo-scoped GitHub token, clones or refreshes the
GitOps workspace and leaves it on a clean `main`, ensures the audit's labels exist, locates the
stream's open ledger issue, and clears any findings document a crashed run left behind. It creates
**no branch** — there is no report branch. It prints exactly one JSON line:

```json
{
  "issue": 128,
  "repo": "acme/fleet",
  "workspace": "/opt/data/gitops/compliance-audit/acme__fleet",
  "findings_path": "/opt/data/scratch/findings_compliance-audit.json",
  "pending_remediation_requests": ["netpol-missing-payments"],
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

`workspace` is the clone. **Every `remediation.path` is resolved against it**, so a manifest written
anywhere else is a file the harness will never find — the finding degrades to a manual one and no
pull request opens. `start` scrubs that directory before handing it to you; `finish` does not, which
is what lets the files you write in between survive.

`pending_remediation_requests` lists the findings a repository writer has already asked to be fixed,
parsed from the ledger's comments. **Write those manifests during inspection** — if the finding is
still reproducing at `finish`, its pull request opens immediately instead of a week later.

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

**Do not leave unrelated uncommitted work in that tree during an audit.** Opening a remediation pull
request requires switching branches, and the harness forces the switch. It snapshots and restores
every path you declared, and returns you to the branch you started on — but a file it was never told
about is not covered by that guarantee.

### Step 3 — `finish`

```bash
./skills/fleet-audit/scripts/audit_report.py finish --audit <audit-id> --findings-file <findings_path>
```

The script validates the document, reconciles every finding against the pull requests already open
for this stream, rewrites (or opens) the ledger issue, comments the delta, opens pull requests for
the fixes that qualify, and closes the ones whose findings have stopped reproducing. It prints one
JSON line with nine fields — `status`, `issue_url`, `new`, `resolved`, `prs_opened`, `prs_closed`,
`partial`, `coverage_gaps`, and `silent_ok`:

- `{"status":"OPENED","issue_url":"…","new":7,"resolved":0,"prs_opened":["…"],"prs_closed":[],"partial":false,"coverage_gaps":[],"silent_ok":false}`
  — the stream had no open ledger.
- `{"status":"UPDATED","issue_url":"…","new":2,"resolved":3,"prs_opened":[],"prs_closed":["…"],"partial":false,"coverage_gaps":[],"silent_ok":false}`
  — the existing ledger was rewritten.
- `{"status":"CLEAN","issue_url":"…","new":0,"resolved":5,"prs_opened":[],"prs_closed":["…"],"partial":false,"coverage_gaps":[],"silent_ok":false}`
  — zero findings; the ledger closed as completed and its open fixes closed with it.

Add `--dry-run` to validate and print the rendered ledger body — and every PR body it _would_ open —
to stdout with **zero** git or gh side effects. It applies the same grouping and the same
degradation as the real run, so the branch names it names are the branch names it would create. It
resolves every `remediation.path` against the same `workspace` clone the real run uses, not against
the directory you happen to be standing in, so "the manifest is missing" is a finding of the dry run
and not a surprise at publish time. Use it whenever you are unsure your document is well formed.

Exit 0 means published. **Exit 2 means the run was rejected before publishing anything** — fix what
the message names and re-run; never delete the finding that tripped it. Three things reach exit 2:
the document failed a field rule, the file named by `--findings-file` is missing or is not valid
JSON, or `--audit` is not one of the five ids above. Exit 1 is fatal and means something else broke.

### Partial coverage

`partial` is `true` exactly when the run could not speak for the whole fleet: any entry in
`scope.skipped`, any cluster carrying a `limitations` note, or any cluster whose `checks_run` is
short of the checks that _apply_ to it. `coverage_gaps` says which, and why — so `partial` is `true`
if and only if `coverage_gaps` is non-empty, and you can report from either.

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
  ]
}
```

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

- `check` is **required**, and is the backticked slug in the heading of the SOP check that produced
  the finding. Anything outside that SOP's roster is rejected.
- **Do not write an `id`.** The harness derives it as `<check>.<cluster>.<namespace>.<object>` — one
  grammar for all five streams — lowercasing each part, replacing every run of non-alphanumerics
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
  would break — and an operator types it by hand in `/remediate <id>`.

- `severity` is one of `critical`, `major`, `minor`.
- `namespace` may be empty for cluster-scoped objects.
- `evidence.command` is **required and non-empty**.
- `recommendation` is **required on every finding**, with all three of `action`, `rationale`, and
  `risk` non-empty. See below.
- `remediation.kind` is `manifest`, `gcloud`, or `manual`. `path` is required for `manifest`
  (repo-relative, no `..`, no absolute paths, no glob metacharacters) and forbidden for the other
  two. For `gcloud`, put the exact command in `note` — it is rendered as a runnable block.
- **A `path` is discovered, never invented.** Editing an object means writing over its existing
  declaration. Creating one means writing beside a sibling already applied to the same cluster and
  namespace — grep the clone for `namespace: <namespace>`, then **open the hits and confirm one
  declares an object you observed on the target cluster** before writing beside it. A `grep` for a
  name is kind-blind and matches label lines and shared prefixes, so a hit is not a declaration
  until you have read it. **The parent directory must already exist in the clone**; if no sibling
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
  block, a field named like a secret, a self-identifying token prefix, a PEM header, an
  `Authorization:` value — replacing them with `[redacted by audit_report.py]`. It is deliberately
  conservative and **does not** touch bare base64, because legitimate audit output is full of it.
  Treat the backstop as a seatbelt, not a licence: it will not catch a credential that looks like
  ordinary output.

- Report what the command showed, not what you infer it implies. Inference belongs in `impact`.
- One finding per object. Do not roll up "12 namespaces lack NetworkPolicies" into one finding — each
  gets its own stable id so each can resolve independently.

## What the ledger says about each finding

Every finding renders in exactly one state, computed fresh each run from whether it still reproduces
and what pull request sits on its branch. Nothing is stored between runs.

| State                | Rendered as                           | Meaning                                    | What the harness does                                        |
| -------------------- | ------------------------------------- | ------------------------------------------ | ------------------------------------------------------------ |
| `open`               | `open`                                | Reproduces; no pull request                | Nothing, unless it qualifies for auto-promotion              |
| `pr-open`            | `fix proposed`                        | Reproduces; a fix is open on its branch    | **Nothing.** The pull request is left alone                  |
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

- **`pr-open` is not refreshed.** An open pull request is left exactly as it is, because a reviewer
  may have pushed onto it and a nightly force-push would silently discard their work. The ledger
  links it; the diff is whatever a human last made it.
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

## Remediation pull requests

A pull request is opened for a finding only when its remediation is a `manifest` — there is nothing
to put in a diff otherwise. Two paths lead there:

- **Auto-promotion.** A finding that is `critical`, is a `manifest`, and has no live pull request on
  its branch is promoted automatically by `finish` — **at most five per run**. The surplus is named
  in the ledger as awaiting `/remediate`, so nothing is silently dropped. "Live" excludes a pull
  request the harness itself closed as stale (that one is re-openable) and includes one a human
  closed or merged (those are not).
- **`/remediate <finding-id>`**, or `/remediate all`, commented on the ledger by someone with write
  access to the repository. This path is uncapped: a human asked for that one by name.

Every `/remediate` gets exactly one answer, and the answer is never silence:

- Accepted — one acknowledgement comment on the ledger naming each target and **what happened to
  it**, never a count: the pull request URL, or "already open" and left untouched, or _superseded_
  by a human close written after the request, or that publishing failed and the next run will retry.
  "3 requests processed" is indistinguishable from "3 requests silently dropped".
- Refused — one reply saying why, for a commenter without write access, a `/remediate` naming a
  finding that is not in the current document, or one naming a non-`manifest` finding.
- Refused **on syntax**, likewise once, because a command the parser will not honour is a person
  waiting for a fix that is never coming. `/remediate` is only read at the start of its own line, so
  one written mid-sentence gets a reply pointing that out; one written with no target at all gets a
  reply too, because reading it as `all` would open every promotable pull request the cap allows on
  someone who typed the command and then went to look up the id. Both replies carry the correct
  syntax and the promotable ids — up to ten, then "and N more", since a refusal is help and not a
  second copy of the report.
- Overtaken by a **clean run** — answered anyway, and answered _before_ the ledger closes. A run that
  finds nothing still replies to every unanswered `/remediate` in the thread to say the finding no
  longer reproduces, and whether the ledger is closing or staying open on partial coverage. This is
  the one morning the issue disappears, taking with it the thread the requester would have re-asked
  on, so it is the one morning silence is least affordable. Authorization is not consulted here:
  nothing is being acted on for anybody, and "it no longer reproduces" is equally true and equally
  useful to a commenter without write access.

Two deliberate silences. A mid-sentence `/remediate` from someone _without_ write access gets
nothing: their correctly-typed command would have been refused anyway, and two replies to one
comment that was probably never a command is a bot picking an argument. And a `/remediate` inside a
code span is prose about the command, not an attempt at it — which is why every `/remediate` the
harness itself writes into a comment is backticked. Keep it that way when you quote one: an
unbackticked command in a harness-authored comment is read back on the next run, and a bot that
answers itself never stops.

All of these are guarded by a hidden marker carrying the triggering comment's node id, so a standing
`/remediate` in the thread is answered once rather than every morning forever.

Run the requested targets through the subcommand, which takes `--finding` once per id:

```bash
./skills/fleet-audit/scripts/audit_report.py remediate --audit <audit-id> \
  --findings-file <findings_path> --finding <id> [--finding <id> …] [--issue <n>]
```

**It opens exactly what you name, and nothing else.** The auto-promotion sweep does not ride along:
one `--finding` produces one pull request (or one, shared, for the group that path belongs to), never
five more for critical findings the requester never mentioned and cannot tell apart from the one they
did. Auto-promotion happens in `finish`, where the whole fleet is being reported on anyway.

It prints one JSON line — `status`, `prs_opened`, `already_open`, and `refused`:

- `{"status":"REMEDIATED","prs_opened":["…"],"already_open":["cluster-old"],"refused":["ns-quota"]}`

`refused` names the targets whose remediation is not a readable file inside the clone — either the
audit promised a manifest and never wrote it, or the path does not resolve inside the repository at
all. Both leave nothing to put in a diff; a `SECURITY:` line in the log says which one happened. The
other targets still open — `/remediate all` expands to every **manifest-remediation** id in the
document, and failing the batch over one unwritten file would answer a request for many fixes with
none. Say which were refused when you acknowledge the command.

Exit 2 means nothing was published, for one of three reasons — read the message before reporting
which: a named id is not in the document at all, a named target is not a `manifest`, or _every_
named target was refused because its file is not readable inside the clone. The first two are fixed
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

The ledger's last section, **How this run checked the fleet**, is a collapsed table of every
`checks_run` entry — cluster, check, command. It is rendered last, against whatever budget the
findings left, and is dropped whole rather than half if it does not fit: a partial evidence table
reads as a short one, and "this run ran three checks" is a worse lie than saying nothing. You do not
write this section; you supply the commands and the harness publishes them.

## The clean run

If the audit finds nothing, still call `finish` with `"findings": []` and a populated
`scope.clusters`. With complete coverage the harness answers any `/remediate` still standing
unanswered in the thread, comments the date and the clusters covered, closes the ledger issue **as
completed**, and closes every remediation pull request still open for the stream. The answers come
first, deliberately: a reply posted after the close would land on an issue nobody is watching.

**Zero findings plus a coverage gap is not a clean run, and it is not silent even on a stream with
no ledger.** With gaps, the harness opens one — titled `coverage incomplete (n gaps, 0 findings)`
rather than the all-clear phrasing — so the run leaves a durable artifact saying what it could not
see. Without this, a stream that inspected nothing produced no issue, no comment, and nothing to
notice: four streams did exactly that on 2026-08-03, and the only reason it surfaced is that a fifth
happened to have a ledger open from the day before.

A clean run is usually not news, and the closed issue is the record — but "clean" alone does not
decide it. **`finish` decides it, and returns the answer as `silent_ok`.** Read the flag; do not
reassemble it from `status`, `new`, `resolved`, and `partial` yourself. That arithmetic has more
clauses than it looks like it has, and re-deriving it is how a run talks itself into silence it has
not earned — on 2026-08-03 a run with two partially-covered clusters answered `[SILENT]` and its
ledger URL never reached the operator who had asked for it.

> **`silent_ok` is `true` only when the run moved nothing an operator needs to hear about:** nothing
> new, nothing resolved, no coverage gap, and no remediation pull request opened or closed.

Two rules follow, and they are the whole rule:

- On a **scheduled** run, `silent_ok: true` → the final response is exactly `[SILENT]`. Otherwise
  report, and every report carries `issue_url` in full.
- **An on-demand run is never silent.** `silent_ok` is the _scheduled_ verdict — it answers "would a
  channel want this?", and it cannot know a person asked. If someone dispatched this job, from a
  kanban card, from chat, or from `cronjob(action='run')`, they are waiting on the answer and
  `[SILENT]` throws it away. Report the outcome and the ledger URL whatever the flag says.

Two clean runs come back `silent_ok: false`, and both matter:

- **`resolved > 0`** — the fleet was carrying findings yesterday and is not today. Something got
  fixed, and that is the best thing this audit ever gets to say. Reporting `partial` failures while
  swallowing this one would leave the operator hearing only bad news.
- **`partial: true`** — the ledger stayed open because the fleet was not fully read. "I found
  nothing" and "I could not look" must not arrive as the same silence.

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
- **Every `remediation.path` stays inside the clone, and the harness proves it twice.** The string
  must be repo-relative with no `..`, no glob metacharacter, and no leading `:` — and before the
  file is read or staged it is re-resolved against the `workspace` root, where no path component may
  be a symlink and the resolved path must sit under the resolved root. Do not create a symlink in
  the clone and point a remediation through it: `manifests/vendor/x.yaml` is beyond reproach until
  `manifests/vendor` is a link to `/etc`, and then the contents of a file outside the repository are
  committed to a public pull request. Nothing is read from a path that fails either test. The
  finding degrades to `manual` with a note saying so, the run logs a `SECURITY:` line naming the
  path, and the report still publishes — but no pull request opens for that finding until the path
  is a real file inside the clone.
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
