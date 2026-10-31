# fleet-audit — The findings document and the evidence rules

Part of the `fleet-audit` skill (`/opt/data/profiles/platform/skills/fleet-audit/SKILL.md`), which says when to open this file.

## The findings document

```json
{
  "audit": "compliance-audit",
  "scope": {
    "clusters": [
      {
        "name": "acme-prod/us-east1/prod-us-east",
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
        "name": "acme-prod/us-central1/prod-autopilot",
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
            "check": "privileged-container",
            "reason": "GKE Autopilot: admission rejects privileged: true and the SYS_ADMIN capability for in-scope workloads, and this cluster carries no WorkloadAllowlist that would exempt one."
          },
          {
            "check": "host-namespace",
            "reason": "GKE Autopilot: admission rejects hostPID/hostIPC/hostNetwork for in-scope workloads, and this cluster carries no WorkloadAllowlist that would exempt one."
          },
          {
            "check": "hostpath-mount",
            "reason": "GKE Autopilot: admission rejects write-mode hostPath for in-scope workloads and allows read access under /var/log alone, and this cluster carries no WorkloadAllowlist that would exempt one."
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
      "cluster": "acme-prod/us-east1/prod-us-east",
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
      "cluster": "acme-prod/us-east1/prod-us-east",
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
real compliance document would be rejected for the `no-hpa` entry, a check outside its roster. `declared[].check` is validated
against the stream's `declarable` set in `AUDITS` — its posture checks, a subset of the roster — and
`obtainability-audit`, `compliance-audit`, `security-patch-orchestrator` and `fleet-wide-cost-analysis` have one today, because their SOPs have a step that writes the list. A
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
    `kubectl`, `gcloud`, `gsutil`, `bq`, `helm`, or `curl`, or the upgrade audit's readiness reporter
    `fleet_upgrade_report.py`; `echo`, `cat`, `python3 -c`, a call back
    into `audit_report.py`, and anything under eight characters are all rejected. One command per
    entry — the one that produced the evidence, not a summary of your approach.

  An empty list is rejected too, unless that cluster's `limitations` says why nothing ran, or, on a
  collector stream ([finish.md](finish.md#step-3--finish)), every check it is answerable for is in its `checks_not_applicable` with a
  reason and the manifest names it as collected (that one adds no gap).
  Enumerating a cluster and checking nothing on it is not a clean cluster — it is an audit that did
  not happen, and without this field the harness cannot tell the two apart. See
  [Scope, skipped, and limitations](#scope-skipped-and-limitations).

- `checks_not_applicable` is **optional**, and says which checks the cluster's shape rules out.
  Each entry is an object with two required fields:
  - **`check`** — the same slugs `checks_run` uses. An unknown slug, a duplicate, or a slug that
    also appears in this cluster's `checks_run` is rejected: a check either ran or could not.
  - **`reason`** — why the check _cannot_ apply here, naming the property of the cluster that rules
    it out ("GKE Autopilot: admission rejects privileged: true for in-scope workloads, and this cluster carries no WorkloadAllowlist that would exempt one"). Anything
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
      "cluster": "acme-prod/us-east1/prod-us-east",
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
  written down" before it closes one (see [The clean run](clean-run.md#the-clean-run)). Write it only for a
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
- **`finish` adds to each `manual` `note` where the fix belongs, from the collector's candidate.**
  It names the `declaration` file, or else the `release_declaration` file (its next sync or upgrade
  can revert a patch to the live object), or else the `namespace_directory` for a new object (with
  the `resources:` entry that an `overlay` source needs, or the `metadata.namespace` that a
  `cluster` source needs). It skips that location note when your note names the same path and the
  cut below keeps that mention. It also names the `reconciler`, whose next sync or upgrade can
  revert a change made by hand. The notes go in a paragraph of their own after your note. It writes
  no note for a candidate without these fields, and none for a fix that `finish` wrote itself, that
  you declined with `--decline-fix`, or that an open pull request carries. A fix whose pull request
  merged while the finding still reproduces, or that a person closed, gets the notes. It removes
  route notes that your note copied from an earlier ledger, from a note of every remediation kind.
  It closes a code fence that your note
  leaves open. When your note and the route notes together are longer than the note cap, it cuts
  your note and ends it with ` …(truncated)`, so that the route notes fit. The cut does not go into
  the text that `finish` adds after your note when the audit did not write your file, or into the
  text and the copy of your file that `finish` adds before your note when a kustomization does not
  list your file. That copy can become shorter by whole lines to make room for the reconciler note.
  A cut never
  goes into a code block: your first code block and all text after it go, and the text before the
  block is cut. When the route notes still do not fit, the location note goes first; the reconciler
  note always stays. A `--dry-run` can leave out a note the real run adds. Do not copy these notes into your `note`.

### Scope, skipped, and limitations

**A cluster appears in exactly one scope list.** Ask one question:

> Could you read it? **Yes** → `scope.clusters`; name any check that did not run there in that
> cluster's `limitations`, and any check that _cannot_ run there in its `checks_not_applicable`.
> **No** → `scope.skipped`, with a reason.

Nothing goes in both, and nothing in `scope.skipped` may appear in a finding. The validator enforces
both halves. This matters because the alternative produces **false all-clears**: put an Autopilot
cluster in `scope.skipped` because one node-level check cannot apply there, and every real finding
on a cluster you did audit gets suppressed along with it. It also refuses a finding whose `cluster`
is the bare name of a `scope.clusters` entry spelled `<project>/<location>/<name>` (`prod` beside
`acme/us-east1/prod`, or beside two such entries): the cluster is part of the finding's id, so the
bare spelling files a second copy of the finding. Any other `cluster` outside `scope.clusters`,
such as a `project/<id>` target, is not checked against it.

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
`.kube-agents/intent.yaml` names, and `finish` moves every finding one covers here itself: a
case-blind lookup on `(check, cluster, namespace, object)`, then on the fleet-wide
`(check, namespace, object)`, compared as the finding id is (`deployment/api` joins `Deployment/api`),
the finding's `cluster` and `title` kept and the note's `repo`, `path` and title as the declaration.
Two slugs name both a posture and a fault, and the join moves only the posture: for
`hpa-cannot-scale` the `min == max` shape, read off the severity the SOP fixes for it (`major`); for
`netpol-missing` the `Namespace/<ns>` shape. A declaration matching the `minor` dangling-target fault
or an allow-all `NetworkPolicy/<name>` is reported on stderr and not applied.
The worker's half is the `provisioning/` pins HCL and YAML make in the GitOps clone, which have no
machine-readable form yet; a match there is moved here by the worker with the lines that pin the
property as `excerpt`. A posture a `declares:` note covers is written to `findings` like any other
and the join moves it; a candidate the worker leaves out because it found the note itself is
declared by `finish` from the collector's manifest when a note covers it, and logged as such.

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
  set in `AUDITS`, four for obtainability, two for compliance, six for the patch stream and eight for the waste audit, and the validator rejects any other check with exit 2. A
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
  declaration covered. A run on which none of the stream's declarable checks ran anywhere owes nothing.
- **Anything less is no search, and the postures are withheld.** A union of the worker's list and
  `start`'s that misses a repository, or no run record: `finish` — real and `--dry-run` — takes every finding whose check is
  declarable out of the document, the dangling-target `hpa-cannot-scale` fault included because it
  shares its slug with the `min == max` posture and nothing on this side tells them apart, and adds
  one `coverage_gaps` sentence naming each withheld entry and the repositories not searched. The
  faults publish, the allow-all `NetworkPolicy/<name>` shape of `netpol-missing` among them, since its
  object tells it apart; `declared[]` entries publish.
  `partial` stays `bool(coverage_gaps)`, so the ledger does not close, `resolved` is `0`, no stale
  pull request is retired (except the shield's close (SOP 2.7), which rests on the declaration rather than on this run's reading), and the withheld ids enter no delta block and no remediation pull
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
file named — see the answers list under [Remediation pull requests](remediation.md#remediation-pull-requests).

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
