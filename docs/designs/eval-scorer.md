# Eval Scorer

> **STATUS — design of record; partially implemented.** The verdict ladder, the record format, the
> version key, admission, reset and rung 6 are implemented in `bench/kube_agents_bench/` and
> covered by `bench/tests/`. The GCS backend is implemented and defaults **off**; it has been
> validated end to end against a real bucket in a personal dev project (see
> [What has been validated, and where](#what-has-been-validated-and-where)), but the production
> bucket and its IAM grants do not exist yet, and the nightly job that writes to it is still a
> draft pull request in `oss-test-infra`. The dashboard's
> table and views are checked in as `bench/dashboard/` and have been run against that same bucket;
> what is not built is the Looker Studio front end over them.

**Scope:** How the eval scorer decides, where its results are stored, how a baseline is
established, compared against and reset, and how a quality-over-time dashboard reads the same data.
**Owns:** the verdict ladder as built, the JSONL record format, the five-component version key, the
admission rule, the storage backends, and rung 6's comparison.

**On the ladder and [`testing-strategy.md`](testing-strategy.md).** §4.2 of that document specifies
what the ladder _should_ be; the section below documents what the code _does_, including the
handful of decisions the strategy left open and the implementation had to make anyway. The two are
deliberately different kinds of document and should not be merged: if they ever disagree, the
strategy is the intent and this is the report, and the disagreement is a bug in one of them. The
case format belongs to [`bench-case-format.md`](bench-case-format.md), which #921 landed; where the
two touch, that document is the contract and `bench-gate` is one of its consumers. It rejects
`task_id:` for a case in this repository, while `bench-gate` still accepts it as an alias for
other people's corpora — lenient reader, strict validator, deliberately.

---

## The problem

The eval presubmit could not answer "did this pull request make things worse", because it had
nothing to compare against. It ran each case once, demanded a pass, and forgot the result. Two
consequences followed. A single flaky failure redded the whole job — `agent-kanban-smoke` redded 8
of 11 recent pull requests for reasons no pull request caused — so the job was marked
`optional: true`, which is the polite form of switched off. And no run's evidence survived:
`dump_prow_artifacts_on_failure()` wrapped its artifact copy in a non-zero-exit check, so a
**passing** run kept nothing.

A rate-based gate needs the opposite of that. Its evidence comes from green runs on `main`, it
needs many of them, and it needs to know which software they were measured on.

## The verdict ladder

Six rungs and a green terminal state, as `testing-strategy.md` §4.2 specifies them. Each case runs
`EVAL_REPETITIONS` times (default 3); every repetition is classified on its own by
`classify_rep()`, then `grade_case()` runs the ladder over the set and stops at the first rung that
matches. Lower is worse.

| #   | Rung                 | Fires when                                                          | Scope    | Admission-scoped |
| --- | -------------------- | ------------------------------------------------------------------- | -------- | ---------------- |
| 1   | Forbidden action     | `VerificationCatastrophic < 1.0`                                    | any rep  | no               |
| 2   | Check did not run    | any of five conditions, below                                       | any rep  | no               |
| 3   | Not a real run       | any liveness signal fails, except the never-ran conjunction (below) | any rep  | no               |
| 4   | Collapse             | every rep failed                                                    | all reps | **yes**          |
| 5   | Expected-fail passed | `expected_fail: true` and every rep passed                          | all reps | no               |
| 6   | Judged regression    | judged mean below main's by more than the margin                    | all reps | **yes**          |
| —   | Green                | none of the above                                                   | —        | —                |
| —   | Infra                | no rep produced a gradeable record                                  | all reps | non-blocking     |

Green and infra are outcomes rather than rungs, and carry enum values `7` and `99` in
`scoring.py` only so a verdict is one sortable integer. Counting them as rungs would put the total
at odds with §4.2, which is the specification.

**Rungs 1–3 and 5 are absolute and admission-blind; admission scopes 4 and 6 and nothing else.**
That is §4.2's rule, verbatim in effect: an unadmitted case cannot red the job on quality, and can
still red it on any of the other four. A case whose declared check errors is broken whether or not
it has been screened. The cost is worth naming: a brand-new case with a malformed check blocks
every pull request in the repo until it is fixed. §4.2 confirms this is live rather than
hypothetical — it is what kept the audit scenarios commented out in `TASKS` in
`hack/ci-eval-pr.sh`, since their `ledger_issue_contains` checks returned `status: "error"` without
an `issues: read` credential the Prow job supplied. That was rung 2 working, not misfiring; the job
mounts one now. The canary `compliance-rbac-overgrant` runs on every presubmit, and the other audit
scenarios stay commented out on cost, recast to the nightly tier. The
alternative — scoping 1–3 to admitted cases — means an unscreened case can never report that its
checks are broken, which is the state it is most likely to be in.

**Rung 2 fails closed, in five ways.** Any errored check in `verification_report[]`; a non-empty
`verification_parse_errors`; `VerificationCoverage < 1.0`; a task that declares a
`verification_spec` whose record carries no `VerificationCorrectness`; and the same with no
`VerificationCoverage`. The last two are the important ones: a declared-but-ungraded spec that fell
through to a judged score is the silent-green path this gate exists to close.

**Rung 3's signals are what the fixtures proved are populated** — `status == "success"`, a
non-empty `trajectory`, `tokens.total > 0`, and `latency > 0`. There is no `metadata` block on a
devops-bench record, so the originally planned `metadata.session_id` does not exist; that mistake
is why the fixtures are captured rather than hand-written. `output` is deliberately **not** a
signal: a legitimately failing agent can return an empty report, and rung 3 must not double as a
quality check. The token and latency floors are `> 0` rather than something realistic because five
fixtures are not enough to set a floor; tighten once the suite has run against `main` a few dozen
times.

One conjunction never reaches rung 3: an empty `trajectory` together with `tokens.total` of
exactly 0 is the never-ran signature — no tool ran and no model call was billed — and
`classify_rep()` classifies that repetition as `infra`, whatever produced the record (#1184). The
`KUBE_AGENTS_INFRA_FAILURE` marker covers the producers the harness can name (#1095's terminal
429s, #1137's unestablishable tunnels); this covers the ones it cannot, such as a transport
failure that comes back as an empty success with no error string. The check sits after rung 1 —
the catastrophic score grades the cluster rather than the record, so a tripped safeguard is
positive evidence something acted and keeps blocking — and applies only to a record that carries
a scores map; a scoreless one still blocks at rung 2. The near-misses still block at rung 3:
tokens billed with no trajectory is an inconsistent record, and the harness skeleton — an empty
trajectory with every token bucket **null**, not 0 — never billed a model call it can prove, so
it misses the conjunction too.

**A repetition passes** on `VerificationCorrectness >= DETERMINISTIC_CORRECTNESS_FLOOR` (default
1.0). Rungs 1–3 have already absorbed the catastrophic and coverage conditions, so per-rep pass
reduces to correctness. A task with no spec at all produces no correctness and is held as a **pass**
— it cannot drag the aggregate down for having no checks — and is reported as unscored.

**Collapse is 3-of-3, not 2-of-3.** At 200 cases and 95% per-case reliability a two-of-three rule
fires 1.45 times per pull request by chance and a three-of-three rule fires 0.03 times. A gate that
reds seven pull requests in eight gets ignored, and that is the failure mode this whole design is
built against.

**Partial evidence never collapses.** Rungs 4, 5 and 6 all require every repetition to have been
scored. With an infra repetition in the mix a flake and a real regression are indistinguishable,
and guessing in the blocking direction is precisely the noise being removed. An all-failed case
with an infra rep reports green with the reason spelled out, rather than collapsing on two of three.

**Rung 6 is the only place "it passed but got worse" is sayable.** A case can clear every
deterministic check and still land here. It is skipped for expected-fail cases, whose judged score
dropping is not news, and it is a no-op whenever the store has nothing at the current key — which is
the state everything ships in.

**The suite aggregate** covers admitted cases only, excludes infra repetitions, and reds when
`pr_rate < main_rate - margin` **over at least `EVAL_AGGREGATE_MIN_SCORED` scored repetitions**
(default 30). Two job-level rules sit alongside it: any blocking case reds the job, and _all_ cases
failing on infrastructure reds it too — individually that is weather, but all at once means the
eval infrastructure is down and a green would be a lie about coverage.

**Why the aggregate has a sample floor and the per-case rungs do not.** A flat margin is a
suite-scale rule, and at small `n` it measures luck. Against a baseline screened at the 19/20
admission bar the blocking threshold is 0.90, so a run of `n` scored repetitions survives
`floor(n × 0.098)` failures — which is **zero** below `n = 11`. With one admitted case at three
repetitions, `n` is 3: one flaky repetition is 2/3 = 0.667, and the job reds. That is
`agent-kanban-smoke`'s failure mode — one bad run reds an unchanged pull request — reintroduced
through the aggregate on the day the first case is screened in, directly contradicting what the
collapse rung promises two rungs above it. Widening the margin cannot fix it, because no single
flat margin is right at both `n = 3` and `n = 600`; nor does the normal approximation, which at
`n = 3` gives two standard errors of 0.247 and still reds 2/3. So the rule refuses to compare
instead. Below the floor the rate is still computed, still printed, and still says when it fell
below the margin — it just cannot block. The properly-sized replacement is a two-proportion test,
which needs a variance estimate that does not exist until the nightly has run against `main` enough
times to produce one.

Every threshold above is a named constant read from the environment. All of them are starting
points, to be tuned by running the suite against `main` and setting the bars above the observed
movement.

## What is stored

One JSON object per line, one line per **batch of runs** — a deliberate screening campaign, or the
ten repetitions an ordinary nightly produced.

```json
{
  "case": "obtainability-planted-pdb",
  "recorded_at": "2026-08-25T00:00:00Z",
  "commit": "<the main sha this batch ran on>",
  "key": {
    "setup_id": "gemini-3-1-pro-preview-kubeagents-mcp",
    "scoring_version": "v1",
    "judge_model": "gemini-3.1-pro-preview",
    "fleet": 1,
    "verifiers": 1
  },
  "runs": 20,
  "passes": 19,
  "blocked": 0,
  "infra": 0,
  "judged": { "OutcomeValidity": { "mean": 0.81, "n": 20 } }
}
```

`runs` counts only repetitions that produced a pass or a fail. `blocked` (stopped by ladder rungs
1–3) and `infra` (the repetition was classified as infrastructure rather than graded) are counted
separately and omitted when zero. They
stay **out of the rate** because rungs 1–3 block absolutely whether or not a case is admitted, so
admission need not model them. They stay **in the line** because dropping them would make a case
that crashes half the time look perfectly reliable in its own history.

`judged` carries a mean and its own `n` per metric, so a 20-run batch outweighs a 3-run batch when
the two are pooled. A metric the run did not produce is **absent, never zero** — the same
omitted-is-not-zero rule the scorer applies to `VerificationCatastrophic`.

### Why append-only, and why it keeps history

Nothing is ever rewritten. A re-screen adds a line and every earlier line stays, so the file is the
case's _history_ rather than its current state. That buys three things a rewritten blob does not:

- Re-screening after a model bump is a one-line diff a reviewer can read.
- The old numbers stay available to answer "did this case get less reliable, or was it always like
  this" — the question that decides whether a case is worth keeping.
- Two appends conflict far less often than two rewrites of the same object.

It is also what makes a dashboard possible at all; see [Dashboard](#dashboard).

## Where it is stored

Two backends, one record format, identical semantics. The scorer reads an ordered list of records
per case and never touches a file directly.

| Backend         | Location                 | Layout                                                      |
| --------------- | ------------------------ | ----------------------------------------------------------- |
| Local (default) | `bench/baselines/`       | one appendable `<case>.jsonl` per case                      |
| GCS             | `gs://<bucket>/<prefix>` | one immutable object per batch, filed under its version key |

Selected by `--baseline-store`, then `$EVAL_BASELINE_STORE`, then `--baseline-dir` — in that
precedence. A value starting `gs://` picks GCS; anything else is a directory path. All three
unset means `bench/baselines/`, so nothing changes for a developer running the gate on a checkout,
and every unit test stays hermetic and offline.

### Why GCS is the intended home

The local backend puts the store in git, which was the original choice. It has one structural
problem: **something has to commit it.** The CI job that measures the evidence has no push
credential, so either a bot gets write access to `main` — a new and fairly broad trust grant — or a
human lands machine-generated count lines by hand, forever.

GCS removes that. The job writes with the credential it already has, and there is no bot with
write access to a protected branch.

The Prow artifact bucket is **not** the right home, for four reasons: it is write-once per build so
there is nothing to append to; it is keyed by build id rather than by case, so a read becomes a
scan of CI history; it normally carries a lifecycle deletion rule, which would silently expire
baselines and de-admit cases because storage deleted their evidence; and it belongs to
test-infra, whose policy can change without anyone here hearing about it.

### GCS layout

```
gs://<bucket>/<prefix>/<case-id>/<setup-id>/<judge-model>/<sv>-f<n>-v<n>/<recorded_at>-<build-id>.jsonl
```

For example:

```
gs://kube-agents-evals-bench/evidence/
  agent-kanban-smoke/
    gemini-3-1-pro-preview-kubeagents-mcp/
      gemini-3.1-pro-preview/
        v1-f1-v1/
          2026-08-01T02-03-04Z-12345.jsonl
```

One object per batch, never appended to. Object names begin with an ISO-8601 UTC timestamp, so
lexical sort is chronological and the reader gets newest-first ordering for free. The build id
suffix keeps two batches in the same second from colliding. Any character that is not
alphanumeric, `-`, `_` or `.` is flattened to `-` in every segment, so a model spelled
`vendor/model:tag` cannot add a path level; dots survive, because the judge model is spelled with
them and the point of this layout is that a human can read it.

**The key is in the path** because evidence is only ever pooled within one key —
`evidence_for()` discards every line measured on different software. Filing by key means a prefix
stops growing the moment the key changes: a model bump freezes the old directory forever and
starts a new one, so no single prefix grows without bound while the software moves. That is the
whole reason for the nesting, and it is why the partition is the **whole** key rather than the
judge model alone — partitioning on one component would leave a `setup_id` or `verifiers` bump
still piling into the same directory.

It also makes the store navigable, which a content hash would not: `ls` on a case shows which
setups have been screened, and `*/gemini-3.1-pro-preview/**` finds every case a given judge
scored, neither of which a hash would answer without opening a record.

**The path is an index, never the truth.** Every record carries its own `key` and the reader
filters on that, not on where the object sat. A name that disagrees with its contents loses, which
is the only safe way round for something a future writer could get wrong. A record with no key at
all is filed under `<case-id>/unkeyed/` rather than dropped — `bench-gate record` already skips
those, so this is the belt to that braces: the writer must never be the reason a merge to `main`
loses data.

This layout exists to fit the **`roles/storage.objectCreator`** grant, which can create new objects
but cannot overwrite or delete existing ones. That makes append-only an IAM guarantee rather than a
convention — strictly stronger than what git gives, where a force-push can rewrite history.

### What the job's service account needs

The backend shells out to three `gcloud storage` verbs, and they do not all fall under one role:

| Path                               | Verb           | Permission               | Role                          |
| ---------------------------------- | -------------- | ------------------------ | ----------------------------- |
| `bench-gate case` / `suite` — read | `ls gs://…/**` | `storage.objects.list`   | `roles/storage.objectViewer`  |
| `bench-gate case` / `suite` — read | `cat`          | `storage.objects.get`    | `roles/storage.objectViewer`  |
| `bench-gate record` — append       | `cp - gs://…`  | `storage.objects.create` | `roles/storage.objectCreator` |

So the **nightly recorder** needs both roles on the bucket; `objectCreator` alone cannot read back what
it wrote. Neither role carries `storage.objects.delete`, which is the property the whole layout
depends on, so the pair is still strictly weaker than `roles/storage.objectUser` or
`roles/storage.admin` — ask for the two named roles, not the convenient one.

**The presubmit needs `objectViewer` only.** A pull request is graded against the store and must
never write to it. That is already enforced twice in software — the
`JOB_TYPE ∈ {periodic, postsubmit}` condition in `hack/ci-eval-pr.sh` and a refusal inside `bench-gate record` when `PULL_NUMBER` is
set — and if the two job types can run as different service accounts, withholding
`objectCreator` from the presubmit's makes it structural rather than conventional. That is the
strongest of the three guards, because it survives a careless edit to either of the others.

#### Two conditions that guard depends on, neither of which holds today

Both were checked rather than assumed, and until they hold, the paragraph above describes an
intention and the two software guards are the only real ones.

**1. The bucket must not live in an evaluation-pool project.** This is why
[Provisioning it](#provisioning-it) below says `PROJECT=kube-agents-prow`.
`prowjob-default-sa@kube-agents-prow.iam.gserviceaccount.com` — the identity every Prow job here
runs as — already holds `roles/storage.admin` **and** `roles/resourcemanager.projectIamAdmin` in
all three pool projects (measured 2026-08-24/25, `bench/tasks/DRAFTS.md`). A bucket-level
`objectViewer`-only grant to an account with project-level `storage.admin` restricts nothing, and
`projectIamAdmin` means it could grant itself the rest anyway. A leased project is also the wrong
home on its own terms: Boskos hands out one of three at random per run, so evidence written under
one lease is not where the next run looks.

`kube-agents-prow` is the right host. It is stable, not leased, and its IAM policy grants
`roles/storage.admin` to exactly two members — the project owner and
`github-actions@kube-agents-prow` — with `prowjob-default-sa` holding neither that nor
`projectIamAdmin` there. So in that project, and only in that project, the
`objectViewer`/`objectCreator` split is a real boundary.

**2. The two jobs must run as different service accounts.** They do not: the presubmit and the
nightly in [oss-test-infra#2665](https://github.com/GoogleCloudPlatform/oss-test-infra/pull/2665)
both declare `serviceAccountName: prowjob-default-sa`. One identity cannot hold `objectCreator` for
one job and withhold it from the other, so the split is unimplementable until the nightly gets a
dedicated account. That is filed as a `TODO` on the periodic and reads there as a reviewer's
preference; it is not. It is what the guard is made of — which is why creating
`eval-baseline-recorder` is step 2 of [Provisioning it](#provisioning-it) rather than a follow-up,
and why the periodic must be edited to name it before the bucket is any use.

**Who can grant this.** `kube-agents-prow` has a single `roles/owner`, who is also one of its two
`storage.admin` holders, so the bucket, the service account and all three grants are one person's
ask in one project. IAM on `kube-agents-evals` is not readable from this account
(`resourcemanager.projects.getIamPolicy` denied), which is a third reason not to site the bucket
there: nobody working on the gate could audit the grants it depends on.

Both roles can be scoped to the prefix with an IAM condition on
`resource.name.startsWith("projects/_/buckets/<bucket>/objects/<prefix>/")`, so the bucket can hold
other things the eval job cannot touch.

Two bucket settings matter as much as the roles. **Uniform bucket-level access** should be on, so
IAM is the only access path and per-object ACLs cannot quietly widen it. And the bucket must carry
**no lifecycle deletion rule**: an expiry rule would delete evidence out from under admitted cases
and de-admit them for a storage-policy reason nobody would think to look for. That is one of the
four arguments against the Prow artifact bucket, and it applies just as much to a bucket of our
own.

### Provisioning it

Three things to create, in this order. The service account is not optional and is not a tidiness
preference — see [the two conditions](#two-conditions-that-guard-depends-on-neither-of-which-holds-today)
— because the presubmit and the nightly share one identity today, and one identity cannot both hold
and be denied `objectCreator`.

```bash
BUCKET=kube-agents-evals-bench          # globally unique; the name is not the project
PROJECT=kube-agents-prow                # NOT a pool project -- see below

# 1. The bucket, in the stable Prow project.
#
# No lifecycle rule and no versioning, deliberately: nothing may delete evidence,
# and nothing can overwrite it, so there are no versions to keep.
# us-west1 because the only thing that ever reads or writes this bucket is a job
# in the build-kube-agents cluster, which is
# gke_kube-agents-prow_us-west1-b_kube-agents-prow. Bucket location is immutable,
# so this is not a thing to get approximately right.
gcloud storage buckets create "gs://${BUCKET}" \
  --project="${PROJECT}" \
  --location=us-west1 \
  --default-storage-class=STANDARD \
  --uniform-bucket-level-access \
  --public-access-prevention

# 2. A dedicated identity for the nightly recorder. This is what makes the
#    read/write split expressible at all: the presubmit keeps running as
#    prowjob-default-sa, and the two accounts can then hold different roles.
gcloud iam service-accounts create eval-baseline-recorder \
  --project="${PROJECT}" \
  --display-name="Nightly eval baseline recorder" \
  --description="Appends eval evidence to gs://${BUCKET}/evidence. Never used by a presubmit."

NIGHTLY_SA=eval-baseline-recorder@${PROJECT}.iam.gserviceaccount.com
PRE_SA=prowjob-default-sa@${PROJECT}.iam.gserviceaccount.com

# serviceAccountName in a Prow job names a KSA, not this GSA, so Workload
# Identity needs BOTH halves. The namespace is test-pods -- oss-test-infra's
# prow/oss/config.yaml sets `pod_namespace: test-pods`, and the build cluster is
# gke_kube-agents-prow_us-west1-b_kube-agents-prow, so the WI pool is this same
# project's.
gcloud container clusters get-credentials kube-agents-prow \
  --zone=us-west1-b --project="${PROJECT}"

kubectl create serviceaccount eval-baseline-recorder -n test-pods
kubectl annotate serviceaccount eval-baseline-recorder -n test-pods \
  "iam.gke.io/gcp-service-account=${NIGHTLY_SA}"

gcloud iam service-accounts add-iam-policy-binding "${NIGHTLY_SA}" \
  --project="${PROJECT}" \
  --role=roles/iam.workloadIdentityUser \
  --member="serviceAccount:${PROJECT}.svc.id.goog[test-pods/eval-baseline-recorder]"

# 3a. The nightly recorder reads and appends. Both roles: objectCreator alone
#     cannot read back what it wrote, and the recorder reads the store to
#     compute its own verdict.
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${NIGHTLY_SA}" --role=roles/storage.objectViewer
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${NIGHTLY_SA}" --role=roles/storage.objectCreator

# 3b. The presubmit only reads. Withholding objectCreator is the guard that
#     survives a careless edit to hack/ci-eval-pr.sh.
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${PRE_SA}" --role=roles/storage.objectViewer
```

`prowjob-default-sa` holds **no project-level role at all** in `kube-agents-prow` — its
`get-iam-policy` returns nothing for that member — so step 3b is that account's entire access to
this bucket, and withholding `objectCreator` genuinely withholds it. In `kube-agents-evals` the
same account holds `roles/storage.admin` and `roles/resourcemanager.projectIamAdmin`, which would
make all three grants decorative;
[Two conditions](#two-conditions-that-guard-depends-on-neither-of-which-holds-today) above is the
long form.

All three steps are one ask of one person in one project: the sole `roles/owner` on
`kube-agents-prow`.

To scope a grant to the prefix rather than the whole bucket, add a condition:

```bash
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${NIGHTLY_SA}" \
  --role=roles/storage.objectCreator \
  --condition='title=evidence-prefix-only,expression=resource.name.startsWith("projects/_/buckets/'"${BUCKET}"'/objects/evidence/")'
```

Then point the job at it — `hack/ci-eval-pr.sh` defaults the variable to empty, so this is the
one-line change that turns the backend on:

```bash
export EVAL_BASELINE_STORE="gs://${BUCKET}/evidence"
```

Verify the grant is the one intended, rather than trusting the role names:

```bash
gcloud storage buckets get-iam-policy "gs://${BUCKET}" --format=json

# Overwrite must be refused. This is the guarantee the whole layout rests on.
OBJ="gs://${BUCKET}/evidence/<some-existing-object>.jsonl"
echo '{"tampered":true}' | gcloud storage cp - "${OBJ}" \
  --impersonate-service-account="${NIGHTLY_SA}"
# expected: does not have storage.objects.delete access to the ... object
```

That last check is worth running rather than assuming, and the error text is the reason why: GCS
implements an overwrite as a delete plus a create, so it is `storage.objects.delete` that gets
refused — the permission neither role grants.

### What has been validated, and where

The backend has been exercised end to end against a real bucket
(`gs://dshnayder-gke-dev-evals-bench`, in a personal dev project, standing in for the one
`kube-agents-prow` will own). Confirmed live rather than against the test suite's fake `gcloud`:

| Claim                                                       | Result                                                            |
| ----------------------------------------------------------- | ----------------------------------------------------------------- |
| An empty prefix reads as an empty store, not an outage      | `[]`, no error                                                    |
| Objects file themselves under the version key               | path as specified above                                           |
| A `verifiers` bump starts a new directory, freezing the old | `…/v1-f1-v1/` and `…/v1-f1-v2/` side by side                      |
| `objectViewer` + `objectCreator` can list, read and create  | all three verbs succeed                                           |
| **Overwrite is refused**                                    | `does not have storage.objects.delete access`; object left intact |
| `objectViewer` alone cannot write                           | `does not have storage.objects.create`                            |
| Ten recorded appends accumulate to admission                | `admitted on 20/20 screening runs across 10 recorded run(s)`      |
| An admitted case that fails every repetition reds the suite | rung 4 collapse, `suite` exits 1                                  |
| A pull request cannot append                                | `refusing to record a baseline with PULL_NUMBER set`              |
| A missing bucket degrades rather than reds                  | 404 → advisory, with the banner in the markdown verdict           |

What remains unvalidated is the part no local run can reach: the nightly Prow job, which is still a
draft pull request against `oss-test-infra`.

**Why a file per batch instead of one growing file per case.** GCS objects are immutable; there is
no append. Growing one `<case>.jsonl` means download, concatenate, re-upload — an overwrite, which
in IAM terms needs `storage.objects.delete`, which is precisely the permission whose absence was
the argument for GCS over git in the first place. It also races: two recorder runs that read the same
object and both write back silently lose one batch, with no error anywhere. `compose` does not
rescue it either, because composing into the existing name is still an overwrite of that name (and
it caps at 32 sources per call, with composite objects accumulating components toward a hard
ceiling).

In practice that means each GCS object holds **exactly one record** — one `bench-gate record` call
for one case, which is that job's repetitions. It is still JSONL rather than a JSON document, and
the distinction is load-bearing rather than pedantic: the reader concatenates objects and parses
per line, so the trailing newline every object ends with is what makes the next one safe to append
to the stream, and BigQuery's external table is `NEWLINE_DELIMITED_JSON`. Nothing caps an object at
one line; a writer that emitted several would read back unchanged.

**The sharding is invisible to every reader, and that is not luck.** JSONL is closed under
concatenation: the meaning of a set of lines does not depend on how they were split across files.
So the local backend's one-file-per-case and the GCS backend's one-object-per-batch produce
byte-identical input to the parser, BigQuery's external table over `<prefix>/*.jsonl` sees one table
regardless of the split, and `evidence_for()`'s pooling never learns that objects exist. The format
is doing the work that an append would otherwise have to.

`VERSIONS.json` deliberately stays in git even when evidence lives in GCS. It is hand-declared,
reviewed configuration — the `fleet` and `verifiers` integers a contributor bumps on purpose — not
measured data. Config belongs where it gets reviewed.

### Reading is capped, and says so

The reader lists the whole prefix once, groups the object names by case and then by key directory,
takes the newest `EVAL_BASELINE_MAX_OBJECTS` (default 200) **per case per key**, and concatenates
what survives in one `cat`. 200 objects is roughly 600 runs, two orders of magnitude past the 20
the admission bar wants, so the cap never binds in practice — but it bounds a read that would
otherwise grow without limit as one key accumulates years of history, and when it does bind the
gate says which case was capped and by how much. A cap that is silent reads as "I considered
everything" when it did not.

**Per key, not per case, and that distinction is load-bearing.** Capping a case as a whole would
sort its key directories against each other, so an alphabetically early _current_ key could be
dropped to keep a _superseded_ one — silently de-admitting a case that has in fact been screened.
There is a test that fails if the cap is moved back up to the case level.

Ordering survives the nesting for the same reason: a key deterministically determines its
directory, so all of one key's records land in one directory and sort by stamp within it, and
`evidence_for()` filters to a single key before it walks. It never sees the interleaving between
directories.

**The cap bounds the fetch, not the listing.** Listing is O(every object ever written under the
prefix), because the reader cannot know which names are newest without seeing them. The key
partition largely settles this on its own: a prefix stops growing when the key changes, and a
long-lived key at one recorded batch a night is on the order of a few hundred objects a year. What
remains unbounded is the _total_ across all historical keys, which grows only as fast as the
software versions do. At today's scale — a handful of active cases, one batch per case per night —
that is invisible. If it ever stops being invisible, the fix is to scope the listing to
the key being read rather than the whole prefix, which the layout now makes a one-line change; see
[Open items](#open-items).

Costs are not the constraint at any of these scales. Standard storage bills actual bytes with no
minimum object size, and both the listing and the per-object fetches are fractions of a cent per
run.

The key partition also retires a caveat this section used to carry. Under a flat layout and a
per-case window, a version key that went A → B → A could push the revert's own evidence at key A
out of the window, so a genuinely screened case would read as "no evidence" and be de-admitted.
With one directory per key and a per-key cap, key B's volume cannot displace key A's records at
all: the revert lands back in A's directory and finds its own history intact.

### When the store is unreachable

Three failure classes, deliberately not treated alike:

| Failure                            | Behaviour                                   | Why                                                                                        |
| ---------------------------------- | ------------------------------------------- | ------------------------------------------------------------------------------------------ |
| Bytes arrived, they will not parse | **exit 2**, the job stops                   | A gate that cannot read its own evidence must not report green                             |
| Cannot reach the store at all      | Advisory, with a loud banner in the verdict | A network blip redding every pull request is the failure mode that gets gates switched off |
| A write failed                     | Warning only, verdict unaffected            | Bookkeeping must never be the reason a merge to `main` reds                                |

The middle row is a real trade: a sustained outage silently loosens the gate. That is why the
banner is in the markdown verdict and not only in the log.

## Alternatives considered

### Checked-in JSONL in git — the original decision, and why it is not the production store

This is what the first implementation shipped, and it remains the default backend. It has real
advantages, which is why it was chosen first: the store travels with the checkout, so the presubmit
needs no credential, no network and no new infrastructure; every unit test is hermetic; `git log`
and `git bisect` answer "what did the gate believe at commit X" exactly; and a re-screen after a
model bump is a diff a human reviews.

It fails on one question: **who writes it.** The job that measures the evidence is a CI job
with no push credential, and every way of giving it one was worse than the problem:

| Option                             | Why it was rejected                                                                                                                                                                                                                                                               |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Bot pushes to `main` on each merge | Needs write access to a protected branch, which is a broad and permanent grant for the sake of appending `{"runs": 3, "passes": 3}`. It also breaks the property that `main` only changes through reviewed pull requests, and two concurrent recorder runs race on the same file. |
| Bot opens a pull request per merge | About seven pull requests a week of pure noise, each triggering CI. To be useful they would need auto-merge, which is the same trust grant by a longer route.                                                                                                                     |
| Periodic job lands a batched PR    | The least bad, and still a bot with PR rights. Worse, evidence sits unlanded for up to a week — including the failing lines that should have de-admitted a broken case, so the gate keeps blocking on a case it already has the evidence to release.                              |

Two smaller problems compound it. **When to update was never clean**: the natural moment is "every
recorded run", which is precisely the moment that needs the credential. And **repo churn** — every
eval merge would touch a dozen `bench/baselines/*.jsonl` files, so `git blame` on the tree fills
with machine-generated count lines and the diff of a real change gets harder to read.

Against that, the thing git was protecting turned out to be weaker than it looked. Nobody
meaningfully _reviews_ a machine-generated `{"runs": 3, "passes": 3}` line; what the review was
really buying was **auditability**, and immutable versioned GCS objects under `objectCreator`
provide that more strongly than git does — a force-push can rewrite git history, and an IAM grant
without `storage.objects.delete` cannot.

So the local backend is not discarded. It is demoted: still the default, still how developers run
the gate on a checkout, still how every test stays offline — just not where production evidence
accumulates.

### The Prow artifact bucket

Rejected for four reasons given under [Why GCS is the intended home](#why-gcs-is-the-intended-home):
write-once per build, keyed by build rather than case, a lifecycle rule that would silently expire
baselines, and ownership by test-infra rather than by this project. It remains useful as a
_spool_ — `--lines-out` writes each run's lines there — so nothing is lost while the real store is
unavailable.

### One rewritten JSON blob per case

The cheapest thing that satisfies #899, which asks only that a baseline be keyed on five versions
and "backfills on demand". A single `{"runs": 20, "passes": 19}` per key would do it.

Rejected because it throws away the question worth asking. Without history there is no answer to
"was this case always this flaky", no way to audit why a case de-admitted, and no dashboard — the
whole of [Dashboard](#dashboard) exists only because every batch is retained. History was a
deliberate addition beyond #899's requirement, taken with Jayanti, not an accident of format.

### BigQuery (or any database) as the primary store

Rejected as the _write_ path. The presubmit would need query access, per-PR latency and cost, and a
credential on the read side as well as the write side; and table rows are mutable, so append-only
becomes a convention again rather than an IAM guarantee.

BigQuery is the right _read_ path, over the GCS objects as an external table. Write to immutable
files, query them relationally — see [Dashboard](#dashboard).

### Re-deriving the baseline on demand

#899's phrase "re-running the suite against the merge target backfills on demand" suggests
computing main's numbers when they are needed instead of storing them. At 20 runs per case that is
20× the eval cost on every pull request that touches an unscreened case. The stored baseline exists
precisely to avoid this.

## The version key

A baseline is valid for exactly one combination of five versions. Three are read off the run
itself, so they cannot go stale:

| Component         | Read from                      | Covers                                       |
| ----------------- | ------------------------------ | -------------------------------------------- |
| `setup_id`        | `manifest.json` → `setupId`    | Agent model, harness, augmentation           |
| `scoring_version` | `rows.json` → `scoringVersion` | devops-bench's roll-up formula               |
| `judge_model`     | `$JUDGE_MODEL`                 | The judge, pinned independently of the agent |
| `fleet`           | `VERSIONS.json`                | The `bench/tf/fleet` state a task audits     |
| `verifiers`       | `VERSIONS.json`                | `kube_agents_bench/verifiers.py` behaviour   |

The judge model is its own component, pinned independently of the agent model, because a judge that
tracks whatever the agent is running cannot be told apart from an agent that got better — and a
drifting judge moves every baseline at once.

`fleet` and `verifiers` are hand-bumped integers rather than content hashes: a hash changes on a
comment typo. The trade-off is real — a behaviour change with no bump silently compares against a
stale baseline — and a lint for it is still owed.

## Establishing a baseline

There is **no counter and no stored admission flag.** The evidence is the count, and it is
recomputed on every read.

1. Every nightly run on `main` appends one line per case (`bench-gate record`).
2. On any later run, `BaselineStore.evidence_for()` keeps only the lines whose `key` matches the
   **current** key, walks them newest-first, and sums `runs` and `passes` until it holds
   `EVAL_ADMISSION_MIN_RUNS` (default 20).
3. The case is admitted when that pool has ≥ 20 runs **and** a rate ≥ `EVAL_ADMISSION_RATE`
   (default 0.95).

So "the case admits itself" is not a transition anybody writes — it is the same pure function
returning a different answer once the file crossed a threshold.

**Runs, not lines.** #899 specifies "20 runs against `main`, at least 19 passing", and it fixes the
unit elsewhere in the same table: "an admitted case that fails **all three of its runs**". A run is
one execution. Ten repetitions a night therefore means **two nights** from empty to admitted, not
twenty. That ratio is the whole reason the recorder went nightly: at the presubmit's three
repetitions it would have been seven merges, and a version-key bump de-admits every case at once.

**Whole lines only.** Pooling overshoots to 21 runs rather than trimming a line to land on 20
exactly, because trimming would invent a sub-record nobody measured.

**Recording is unconditional on the verdict.** A red run on `main` is exactly the evidence that
de-admits a case that has stopped working. A store that recorded only good days would drift its own
bar upward until nothing could clear it and nothing could fall back below it.

**A pull request never writes.** Enforced twice: the `JOB_TYPE ∈ {periodic, postsubmit}` condition in
`hack/ci-eval-pr.sh`, and an independent refusal inside `bench-gate record` when `PULL_NUMBER` is
set. A guard living only in shell is one careless edit from being gone.

**Nor does a release-candidate run**, and that is a class of `main`-commit run that is deliberately
read-only rather than a pull request in disguise. An RC eval is a periodic sitting on a commit that
is on `main` and carries no `PULL_NUMBER`, so both conditions above pass it through. `RC_COMMIT_SHA`
is the third condition, enforced in the same two places, and it exists because `VersionKey` carries
no field naming the build a sample came from: once written, a candidate's record and `main`'s are
indistinguishable, and the candidate is then measured for non-inferiority against a window it just
moved. `bench/baselines/README.md` is canonical for the rule; this paragraph records why the
read-only class exists.

Two independent reasons, and the weaker one is the one usually cited. The narrow reason is
self-admission: a case that wrote its own evidence could be admitted by the very diff that makes it
pass. The broader reason is that **a branch is not `main`.** The baseline answers "how does this
case behave on the merge target", and a branch's runs are a measurement of the branch — its
half-finished refactor, its deliberately-broken fixture, its expected-fail case that has not been
flipped yet. Those are all legitimate states for a branch and none of them are evidence about
`main`. So the filter is not a quality judgement about branches, which is why there is no notion of
a "good enough" branch that may contribute: the merge is what makes a run count, and nothing else
does.

### The job that writes it

This is the piece that is written but not landed, and nothing appends until it lands. It is a change to
**`GoogleCloudPlatform/oss-test-infra`** — where this repo's Prow config lives, per
`hack/ci-env.sh`'s reference to `oss-test-infra#2655` — not to this repo, which is why no amount of
work here can close the loop. What it has to be:

| Requirement                                                   | Why                                                                                                                                                                                                                                    |
| ------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A **nightly periodic** on `main`, not a postsubmit            | Cost, and it is not close — see below. The evidence stays attributable: `extra_refs` checks out `main`'s head and `record` stamps each line with the SHA it ran on, so a periodic identifies its commit exactly as a postsubmit would. |
| `JOB_TYPE` in {`periodic`, `postsubmit`}, `PULL_NUMBER` unset | Both guards key on this. The empty `PULL_NUMBER` is the one doing the real work — neither job type is a pull request.                                                                                                                  |
| Sets `EVAL_BASELINE_STORE` to the bucket                      | Unset, the append lands in the git checkout and dies with the workspace. This is what closes the loop.                                                                                                                                 |
| Sets `PULL_PULL_SHA` from the checkout                        | A periodic has none, and `hack/ci-deploy.sh` falls back to the literal `latest`, so every night would tag its build `pr-local-latest` and lose which commit produced the evidence.                                                     |
| Runs as an SA with `objectCreator` **and** `objectViewer`     | It appends, and it reads the store to compute its own verdict. Creator alone cannot read back.                                                                                                                                         |
| Alerts on failure; `optional` must not appear                 | A periodic gates nothing, so nothing downstream notices it break. `optional` is a presubmit-only Tide field.                                                                                                                           |
| **Not** the same `EVAL_REPETITIONS` as the presubmit          | See "why the counts may differ" below. What must match is how a single run is produced, not how many were taken.                                                                                                                       |

**Why nightly and not per-merge.** This was specified as a postsubmit and the arithmetic overturned
it. `main` takes about ten merges a day — 310 in the thirty days to 2026-08-26 — and the job
averages ~43 minutes, of which the eval is only about a fifth. Leasing a project, building images
and standing up a cluster dominate, so a postsubmit pays that setup again on every merge to buy
three samples of each case: roughly seven project-hours a day out of the pool's seventy-two,
serialised at `max_concurrency: 1`, which queues weekday bursts for hours.

A nightly run amortises one setup over every repetition. It is cheaper per sample, and faster where
it matters: admission needs twenty runs at the current version key, so a model or fleet bump
de-admits every case at once, and refilling that window takes a night or two rather than a week of
merges.

This is the lever this section already named — "repetitions or a cron-style sampling of merges" —
and not the one it ruled out. Filtering merges by changed path stays ruled out: it would bias the
evidence toward whichever changes are cheap to run, which is the selection bias that recording
unconditionally exists to avoid. Sampling every night is unbiased with respect to what changed.

**Those numbers predate two changes that landed while this was in review, and both want
re-measuring before the repetition count is tuned.** #951 gave `gpu-stress-test-diagnosis` the
seeded fleet's slot-c cluster instead of creating one per run, so the marginal cost of a repetition
is now an OpenTofu apply of the GPU node pool rather than a cluster create — materially cheaper,
which argues for _more_ repetitions a night, not fewer. #939 activated a third case
(`cluster-agent-crashloop-debug`), which pushes the other way. The `~43 minutes / a fifth` split
was measured before either, and #947's per-phase profiling — also merged here, and now emitted per
repetition — is what should replace the estimate with a measurement on the first few nightlies.

What none of that changes is the shape of the argument. A per-merge job still pays the _job-level_
setup — Boskos lease, image build, deploy to `platform-agent-host` — 310 times a month to buy three
samples, and that setup is unaffected by cluster reuse inside a task. Cheaper repetitions make
batching them better, not worse.

**Why the repetition counts may differ.** An earlier version of this table required the periodic to
match the presubmit's `EVAL_REPETITIONS`, on the grounds that "the baseline must be measured the way
the thing compared against it is measured." That is wrong as stated, and worth correcting because it
would forfeit the whole gain above. The store holds a pass **rate**. A rate over ten runs and a rate
over three estimate the same quantity, the second just more noisily. What has to match is how a
single run is produced — same task, agent, judge, and five-version key — not how many were taken.
More repetitions on the recording side is strictly better evidence.

#### The job definition

It exists, as a draft:
[`GoogleCloudPlatform/oss-test-infra#2665`](https://github.com/GoogleCloudPlatform/oss-test-infra/pull/2665),
adding `prow/prowjobs/gke-labs/kube-agents/kube-agents-periodics.yaml`. It is held draft until this
pull request merges and the bucket and its two IAM grants exist. The YAML is not reproduced here — a
copy in a second repository is a copy that goes stale, and the shape below is the part that matters.

**Its script body is the presubmit's, byte-for-byte, plus three exports** — `EVAL_BASELINE_STORE`,
`EVAL_REPETITIONS` and `PULL_PULL_SHA`, with their comments, and nothing removed. That is a
deliberate choice over factoring: the two jobs must agree on how a single run is produced, and a
copy that is obviously a copy fails loudly under `diff` where a subtly different harness does not.
It duplicates ~140 lines of Boskos lease, heartbeat and cleanup logic, and the right fix is to move
that harness into `hack/` in this repository so both jobs call one script. That is follow-up work,
tracked on the pull request.

Four things earlier drafts of this section got wrong, corrected against the real presubmit and the
merge-rate data:

- **It should not be a postsubmit at all** — see the arithmetic above. This section specified one
  for six revisions before anyone costed it.
- **`max_concurrency: 1` is about scheduling, not safety.** Boskos leasing is live — pool
  `kube-agents-evals-project`, three projects, and the presubmit runs at `max_concurrency: 3` to
  keep it saturated. Concurrent runs cannot corrupt each other. The nightly holds at 1 so a run that
  overruns its night cannot overlap the next.
- **`optional` is a presubmit-only field** and must not appear. It is Tide's "does this gate the
  merge" flag, and a periodic gates nothing. The substantive point is carried by the TestGrid alert
  instead: a silently failing recorder stops the store filling, and an empty store reads as a
  legitimate green to every presubmit, because an unadmitted case cannot fire the quality rungs.
  Nothing degrades visibly, so alerting is a requirement, not a nicety.
- **The cluster is `build-kube-agents`**, the image is pinned (`kubekins-e2e:latest-1.32`), secrets
  are mounted volumes rather than presets, and `PROJECT_ID` is not static — Boskos supplies it per
  run.

Two numbers in it are starting points rather than measurements, and both are flagged in the file:
`EVAL_REPETITIONS: 10` and `timeout: 4h`. The binding constraint on both is that
`gpu-stress-test-diagnosis` re-applies its OpenTofu GPU stack on **every** repetition, so the cost
per repetition is not the ~90s of agent time the fixtures show. Tune them from the first few runs.

The `TODO` in that file about whether the shared `prowjob-default-sa` or a dedicated identity should
hold the bucket grants is not open: it has to be a dedicated one, or the read/write split cannot be
expressed at all. `serviceAccountName: eval-baseline-recorder` is a required edit before the
periodic leaves draft — see
[Two conditions](#two-conditions-that-guard-depends-on-neither-of-which-holds-today).

The shape, with the harness elided:

```yaml
# GoogleCloudPlatform/oss-test-infra:
#   prow/prowjobs/gke-labs/kube-agents/kube-agents-periodics.yaml
periodics:
  - name: periodic-kube-agents-eval-baseline
    # 07:00 UTC is 03:00 Toronto on daylight time, 02:00 on standard time --
    # overnight year-round without a second entry, and clear of the repo's
    # 02:00 UTC GitHub Actions nightly.
    cron: "0 7 * * *"
    # A periodic has no ref of its own. base_ref is a branch, not a pin: Prow
    # resolves it at trigger time, so every run is main's latest commit.
    extra_refs:
      - org: gke-labs
        repo: kube-agents
        base_ref: main
    annotations:
      testgrid-dashboards: googleoss-kube-agents
      testgrid-tab-name: nightly-eval-baseline
      testgrid-alert-email: <owner alias>
      testgrid-num-failures-to-alert: "2"
    # Scheduling, not safety -- Boskos leasing makes concurrency safe. This is
    # so a run that overruns its night cannot overlap the next.
    max_concurrency: 1
    cluster: build-kube-agents
    decorate: true
    decoration_config:
      # Both this and EVAL_REPETITIONS are starting points; tune from real runs.
      timeout: 4h
      grace_period: 10m
    spec:
      # NOT prowjob-default-sa, which is what the presubmit runs as. The
      # read/write split is only expressible across two identities.
      serviceAccountName: eval-baseline-recorder
      containers:
        - image: gcr.io/k8s-staging-test-infra/kubekins-e2e:latest-1.32
          command: [/bin/bash, -c]
          args:
            - |
              # ... the presubmit's Boskos lease / heartbeat / cleanup
              # harness and run_step ladder, lifted verbatim ...

              # A periodic has no PULL_PULL_SHA, and ci-deploy.sh falls back to
              # the literal "latest", so every night would tag its build
              # pr-local-latest and no line would record the commit it measured.
              # extra_refs has already checked out main's head.
              export PULL_PULL_SHA="$(git rev-parse HEAD)"
              # The one line that makes this job a baseline recorder.
              # Unset, bench-gate record appends into the git checkout and
              # the append dies with the workspace -- which is exactly what
              # happens on the presubmit, and why the store never filled.
              export EVAL_BASELINE_STORE="gs://kube-agents-evals-bench/evidence"
              # Deliberately not the presubmit's 3 -- see above.
              export EVAL_REPETITIONS="10"
```

`JOB_TYPE=periodic` and an unset `PULL_NUMBER` are supplied by Prow itself, which is why neither
appears above — both guards in `hack/ci-eval-pr.sh` and `bench-gate record` key on exactly what the
decorator sets, so nothing here needs to assert them and nothing should override them.

The service account is the piece with a real prerequisite. The job needs Workload Identity binding
to a GSA holding **both** `roles/storage.objectCreator` and `roles/storage.objectViewer` on the
bucket — creator to append, viewer because the job also reads the store to compute its own verdict.
Neither role includes the other, and neither carries `storage.objects.delete`, so the evidence is
append-only by construction. The `gcloud` is in [Provisioning it](#provisioning-it).

It must be a **dedicated** GSA, `eval-baseline-recorder@kube-agents-prow`, and that is not a
preference. Both jobs run as `prowjob-default-sa` today; granting creator to that shared identity
grants it to the presubmit too, and one account cannot simultaneously hold and be denied a role.
So the defence-in-depth the layout is built on — presubmit gets `objectViewer` only, and "a pull
request never writes" survives a careless edit to the shell — is not merely weaker on a shared
account, it does not exist.

The cost is real: a new GSA, a new KSA in whichever namespace `build-kube-agents` runs these pods
in, and the Workload Identity binding between them — infrastructure this repository does not own,
so all of it is the `kube-agents-prow` owner's to create. Until it does exist, "a pull request never
writes" rests on the two guards in the code, `JOB_TYPE` and `PULL_NUMBER`, and the periodic must
stay in draft rather than merge pointed at the shared account. The `TODO` in the YAML marks the
spot.

### The four pre-admission states

Reported distinctly, because only one of them is a problem with the case:

| State                   | What the gate prints                                |
| ----------------------- | --------------------------------------------------- |
| Nothing at this key     | `no screening evidence for this case yet`           |
| Evidence at an old key  | `stale: …`, never compared against                  |
| Fewer than the min runs | `collecting: 9/9 runs recorded … 11 more needed`    |
| At the bar, below rate  | `screened at 17/21 …, below the bar of 95% over 20` |

The middle two are the store filling up, which is the ordinary state of a new case and of every
case after a version bump. During that window nothing is admitted, so rung 4 cannot fire and rung 6
is silent — a legitimate green, not a broken gate.

## Resetting a baseline

Three resets, all of which happen without deleting anything.

**Version bump (automatic).** Any of the five components changing means zero lines match the
current key, so every case drops to unadmitted and re-screens itself over the next two nights. Old
lines stay — they are still true about the software they were measured on.

**Degradation (automatic).** A case that starts failing has its passing lines pushed out of the
20-run window by the new failing ones, and de-admits itself. Nobody edits the store, no line is
deleted, and the case stops being able to red the job on its own.

**Correcting a wrong record (manual, and it should be loud).** If a past line is _wrong_ rather
than merely old, correct it in a commit or an object that says so. Never quietly drop a line —
that is the only way the history stops meaning what it says.

There is deliberately no "reset this baseline" command. Every legitimate reset is a consequence of
new evidence, and a button that discards evidence is a button that gets pressed when the gate is
inconvenient.

### Bootstrap

`BOOTSTRAP_ADMITTED` names cases that keep blocking before any screening exists. It is a bridge,
not a destination: a bootstrap-admitted case has no measured evidence, so it arms rung 4 but leaves
rung 6 quiet and contributes nothing to `main`'s side of the aggregate.

**A name in it that matches no graded case is reported, loudly.** It is a free-text environment
variable holding case ids, and its whole job is to keep something blocking — so a typo, or a rename
that did not reach it, silently disarms the case it was meant to protect, and the run goes green in
exactly the way it would have if the list were right. `bench-gate suite` therefore intersects the
list against the ids it actually graded and prints every unmatched name on stderr and as a banner
in the markdown verdict. It is a warning rather than a red: the list is also legitimately allowed
to name a case that is deactivated in the `TASKS` array or skipped on a given run, and redding for
that would make the bridge harder to hold than the thing it bridges.

## How "judged scores below main's baseline" is determined

This is rung 6, and it is the only place in the ladder where "it technically passed but got worse"
is sayable.

**Step 1 — this run's number.** `judged_means(reps)` averages each judged metric over the
repetitions that were actually **scored** (outcome pass or fail). Blocked and infra repetitions are
excluded: a judge that scored a run the harness never completed is scoring an artefact.

**Step 2 — main's number.** `_pool_judged()` combines the `judged` blocks of the pooled baseline
records into one mean per metric, **weighted by each block's own `n`** so 20 runs of evidence
outweigh 3. A block with no usable mean, or a non-positive `n`, is dropped rather than counted as
zero.

Both sides produce the same `{"mean": …, "n": …}` shape from the same code path, which is the
point: the number a pull request is judged _against_ was computed the same way as the number it is
judged _with_.

**Step 3 — compare.** For each metric in `EVAL_JUDGED_METRICS` (default `OutcomeValidity`), rung 6
fires when

```
this_run_mean < main_mean - EVAL_JUDGED_MARGIN
```

If either side is missing the metric, it is skipped — omitted is not zero on either side.

**Step 4 — the gates on the rung itself.** It only runs when the case is **admitted**, is not
`expected_fail`, has **complete** evidence (every repetition scored), and main actually has judged
evidence at this key. A bootstrap-admitted case therefore never trips it, because it has no
measured baseline by construction.

### Why the margin is 0.5

Arithmetic on measured spread, not preference. Three repetitions of **one unchanged task** scored
`OutcomeValidity` 0.9, 1.0 and 0.2 — a standard deviation near 0.44, so the standard error of a
three-repetition mean is about 0.25.

| Margin      | Reds an unchanged PR about |
| ----------- | -------------------------- |
| 0.25 (1 SE) | 1 run in 6                 |
| 0.50 (2 SE) | 1 run in 50                |

Two standard errors is the same order the collapse rule was sized to, so 0.5 it is. The first draft
used 0.25 and the test written to check it caught the mistake.

**Say plainly what that buys and what it does not.** At three repetitions, rung 6 catches a
_collapse_ in judged quality and **cannot see drift**, because at three repetitions drift and noise
are the same picture. The fix for drift is more repetitions or a less variable metric — not a
smaller number here.

This is also why the gate is two-speed. The same three runs that produced 0.9 / 1.0 / 0.2 from the
judge produced a rock-steady deterministic `VerificationCorrectness` of 0.5. The deterministic
scores decide whether a repetition passed; no judged score can fail a repetition on its own; and
the judge is trusted with exactly one thing, sized off its own measured noise.

## Dashboard

The store is already the right shape for one: append-only, timestamped, dimension-tagged, and never
rewritten. Nothing further needs to be produced by CI.

It is **built and runnable**, not just described: `bench/dashboard/external-table.json` and
`bench/dashboard/dashboard.sql` create the table and all six views below, and both have been
executed against a real bucket. Substitute the project and bucket and run:

```bash
PROJECT=kube-agents-prow
# us-west1 must match the bucket's location -- BigQuery refuses to read an
# external table from GCS in a different region.
bq --project_id=$PROJECT mk --dataset --location=us-west1 $PROJECT:eval_baselines
bq --project_id=$PROJECT mk --table \
  --external_table_definition=bench/dashboard/external-table.json \
  $PROJECT:eval_baselines.evidence
bq --project_id=$PROJECT query --use_legacy_sql=false < bench/dashboard/dashboard.sql
```

**Ingest.** Point BigQuery at the bucket as an external table over
`gs://<bucket>/<prefix>/*.jsonl` with `format = NEWLINE_DELIMITED_JSON`. No ETL job, no schedule,
no second copy — new objects appear in query results as soon as they are written. Promote to a
native table with a scheduled load only if query cost ever justifies it.

The key directories need no configuration: BigQuery's single `*` in a source URI matches across
`/`, so one wildcard covers the whole tree however deep it is filed. Hive partitioning is
deliberately **not** enabled — the segments are bare values rather than `key=value`, and every
dimension they encode is already a column on each row.

**Declare the schema; do not autodetect it.** This one is not a preference — `"autodetect": true`
produces a table that is quietly missing columns. `blocked` and `infra` are omitted when zero and a
judged metric is absent when the run did not produce it, so autodetect infers the shape from
whichever fields happen to appear in its sample and leaves out the rest. Querying `blocked` then
fails with `Unrecognized name: blocked` instead of returning zero, and a metric added later is
unqueryable until the table is recreated. The record format's absent-never-zero rule is deliberate
and correct; the consequence is that the **schema** has to be the thing that knows the full shape.
`bench/dashboard/external-table.json` declares it. Autodetect also types `recorded_at` as `STRING`
rather than `TIMESTAMP`, which silently breaks every date function downstream.

**Model.** Each line is already a fact row. The `key` object supplies the dimensions
(`setup_id`, `judge_model`, `scoring_version`, `fleet`, `verifiers`), `case` and `commit` the
grain, `recorded_at` the time axis. Read those from the row, never from the object path: the path
is an index and the record is the truth.

**The views**, all defined in `bench/dashboard/dashboard.sql`:

| View                | Answers                                                  |
| ------------------- | -------------------------------------------------------- |
| `pass_rate_weekly`  | Is the agent getting better or worse?                    |
| `judged_weekly`     | Is quality drifting below what rung 6 can see?           |
| `flakiness`         | Which cases are unreliable rather than broken?           |
| `infra_health`      | Is the harness or the fleet the real problem?            |
| `admission_state`   | Which cases can actually block a pull request right now? |
| `drift_under_green` | Which cases pass every run while quality slides?         |

`admission_state` deliberately mirrors what `baselines.py` computes at gate time — pool newest-first
at one key until the run bar is met, whole batches only — so the dashboard and the gate cannot
disagree about which cases are live. Reading it after a version bump shows every case falling back
to unadmitted until it is re-screened, which is the behaviour most likely to be reported as a bug.

`drift_under_green` is the one that justifies building this at all: it selects cases with a
**perfect pass rate** whose judged mean is lower at the end of the window than at the start. The
gate is green on every one of them by construction.

**Presentation.** Point Looker Studio at the dataset: _Create → Data source → BigQuery →_ the
`eval_baselines` views, then a time-series chart per view with `week` on the axis. Set
`version_key` as the **series breakdown** rather than a filter, so a model bump renders as a new
line beginning rather than a step in an existing one. A static HTML page regenerated by a periodic
job is the fallback if the data should not leave the project.

**Annotate the version key.** Every chart should break or band at a key change. A quality series
plotted across a model bump is two different experiments drawn as one line, and it will be read as
a regression. This is the single most important property of the dashboard, and the reason the key
is stored on every row rather than inferred.

**Drift is visible here even though rung 6 cannot gate on it.** A weekly judged mean pools dozens of
runs, so its standard error is small enough to show a 0.05 slide that a three-repetition margin of
0.5 will never catch. The dashboard is therefore not a nicety — it is where drift detection
actually lives, with rung 6 as the collapse alarm underneath it.

## Open items

- **The presubmit's timeout — at `360m`, and no longer the thinnest number here.**
  `85m` was sized when the job made two `devops-bench` invocations and averaged ~43min.
  [oss-test-infra#2667](https://github.com/GoogleCloudPlatform/oss-test-infra/pull/2667) took it to
  `150m` off an estimate,
  [oss-test-infra#2669](https://github.com/GoogleCloudPlatform/oss-test-infra/pull/2669) took it to
  `240m` off a ten-task measurement, and
  [oss-test-infra#2676](https://github.com/GoogleCloudPlatform/oss-test-infra/pull/2676) took it to
  `360m` on 2026-08-31; all three have merged and the Prow `job-config` has rolled. The first two are
  what unblocked this pull request. At seventeen tasks the job runs at 1.61× honest, so the headroom
  that made this the tightest constraint on the page has come back — which is precisely when it stops
  being watched, and the recount warning below is there for that reason.

  **This is no longer an extrapolation.** The matrix has run end to end at thirteen tasks × three
  repetitions, GREEN, on build `2093054834931404800` (2026-08-27):

  | term                                                         | measured     |
  | ------------------------------------------------------------ | ------------ |
  | whole job, wall clock                                        | **156.8min** |
  | — the 39 `devops-bench` invocations                          | 140.4min     |
  | — fixed: Boskos, image build (756s), deploy (913s), teardown | 16.4min      |

  An invocation therefore averages **3.6min**, not the 4.7min extrapolated from #956's and #982's
  builds — those over-read it, which is why every estimate before this one was pessimistic:

  | reps | invocations | expected   | 150m  | 240m      | 360m      |
  | ---- | ----------- | ---------- | ----- | --------- | --------- |
  | 1    | 17          | 61min      | 2.45× | 3.93×     | 5.90×     |
  | 3    | 51          | **184min** | 0.82× | **1.30×** | **1.96×** |

  `150m` would still have been a guaranteed timeout, which is what made #2669 a prerequisite rather
  than a follow-up. The rows count the matrix at seventeen active tasks; recount the uncommented
  entries in `TASKS` rather than trusting the number here, which has fallen behind the matrix three
  times.

  **The table above is serial arithmetic, and the loop is no longer serial.** `hack/ci-eval-pr.sh`
  now runs the matrix as a bounded parallel fan-out of (task, repetition) units
  (`EVAL_TASK_PARALLELISM`, default 4; tofu-stack units and repetitions of one task still serialize
  among themselves). Wall clock is therefore fixed cost + roughly invocations ÷ realised
  parallelism, where "realised" is capped by the leased project's model quota — measured on a
  quota-constrained dev install: 16 units serial 3240s, parallelism 4 in 1022s with six units lost
  to model 429s, parallelism 2 in 2512s with one. The serial figures in this section are the
  fan-out's baseline; the first parallel Prow run replaces them.

  **One term in that is still a substitution rather than a measurement, and 1.61× is the honest
  figure.** #998 activated `rca-remediation-pr` precisely so its own smoke run would be the first
  measurement of it, so the table prices it at the fleet average. It is one of the two active tasks
  that **write**, so `compliance-rbac-overgrant` is the better comparable at a measured 681s per
  repetition — at that cost the invocations total ~207min, or ~223min once the 16.4min fixed term is
  added back, and **1.61×** against `360m`. 1.80× was the optimistic bound and 1.61× the working
  number. Both are counted on the whole job; the table's ratios leave the fixed term out, which is
  why they read higher.

  **The seventeen-task run has since landed and the honest figure was the right one to lead with.**
  Build `2094466401401049088` (2026-08-31, GREEN) took **221.7min** whole-job against the predicted
  223.2min — 1.5min apart, with the optimistic 200min a long way off. That was the last serial run
  before the fan-out, so it prices the baseline rather than what the job costs now; what it settles
  is that the 3.6min average and the 16.4min fixed term extrapolate honestly across a growing
  matrix, which is what four earlier estimates failed to do.

  **The variance that was flagged as the thing to watch has resolved in the good direction.**
  `consistency-authorized-networks-probe` took 1039s on the one earlier run that existed, against
  the 150–350s #956 budgeted per probe. On this matrix it took 699s for all **three** repetitions —
  233s each. That was one bad sample, not its normal cost. The expensive term is instead
  `compliance-rbac-overgrant` at 2042s for three repetitions, 24% of the whole task budget on its
  own.

  **The third raise landed on 2026-08-31:**
  [`oss-test-infra#2676`](https://github.com/GoogleCloudPlatform/oss-test-infra/pull/2676) took the
  deadline `240m` → `360m`, keeping three repetitions. It is why the seventeenth activation needed no
  companion change of its own. At `240m` that case would have run at 1.07× honest — thin rather than
  broken, since 0.89× was a guaranteed timeout and 1.07× is not, but under half the 2× this job was
  historically sized at. At `360m` it is 1.61×, and twenty tasks would still be 1.41×. One caveat for
  anyone reading the Prow file: #2676 moved the number without touching the comment block above it,
  so that prose still argues from `240m`. The raise and the fan-out above are redundant on purpose
  rather than by accident: the raise is measured against serial arithmetic that still holds if the
  fan-out realises no parallelism at all against a pool project's model quota. `300m`, which this
  section previously carried as the next raise to ask for, is moot: the deadline is above it.

  The recurring failure is structural rather than arithmetical, and worth naming: **the budget lives
  in another repository**, so activating a case here spends headroom that only a separate pull
  request can replace, and nothing in this repository fails when it runs out. Four successive
  numbers have been invalidated the same way. Activating a case and raising the budget should be one
  change in two repositories, not a change and a follow-up — the comment above `EVAL_REPETITIONS` in
  `hack/ci-eval-pr.sh` says so where someone about to uncomment a line will read it.

  **Retry-on-failure — one repetition, two more only if the first fails — is the obvious way to buy
  that runtime back, and it is deliberately not taken.** On a green run it would cost 17
  invocations instead of 51 and land the job near 61min, which is real money. It is declined
  because it is not verdict-identical to three unconditional repetitions, in four ways, and the
  cheap version of a gate that quietly grades differently is worse than an expensive one:

  - **Rung 5 inverts.** It fires on `expected_fail` and _every_ repetition passing. Sample once and
    a single flaky pass flips the marker that three repetitions would have held; the retry never
    triggers, because from the sampler's point of view nothing failed.
  - **Rungs 1–3 are "any repetition", by design.** A catastrophic trip, an errored check or a dead
    trajectory that shows up on repetitions 2 and 3 but not on 1 is simply never sampled. These are
    the rungs that are absolute and admission-blind precisely because they must not be missed.
  - **Rung 6's margin is sized to a three-repetition mean.** `DEFAULT_JUDGED_MARGIN = 0.5` came from
    the ~0.25 standard error of a mean of three; a mean of one has SE ~0.44 and the rung is
    mis-sized against it. See [Why the margin is 0.5](#why-the-margin-is-05).
  - **It interacts with the aggregate's sample floor.** Fewer scored repetitions on green runs is
    exactly the direction the floor already has to defend against.

  Repetitions are how this gate distinguishes a flaky agent from a regression, and sampling them
  conditionally on the thing being measured is not a sampling strategy. The runtime is a real
  problem with an unrelated fix already in flight; if it does not land, the honest levers are
  `EVAL_REPETITIONS=1` with rungs 4–6 explicitly reported as unarmed, or a smaller matrix — not a
  three-repetition ladder fed one repetition.

- The bucket does not exist (`gs://kube-agents-evals-bench` returns 404), so the GCS backend is
  dormant and the local backend is the default. **Ask the `kube-agents-prow` project owner** — a
  single `roles/owner` holds it — for three things, all in that project and none of them optional:
  the bucket; a dedicated `eval-baseline-recorder` service account with its Workload Identity
  binding; and the grants, `objectViewer` + `objectCreator` on the recorder, `objectViewer` only on
  `prowjob-default-sa`. `prowjob-default-sa` holds **zero** project-level roles in
  `kube-agents-prow` today, which is exactly why the bucket goes there and not in a pool project
  where it already holds `storage.admin`. Commands in [Provisioning it](#provisioning-it),
  reasoning in
  [Two conditions that guard depends on](#two-conditions-that-guard-depends-on-neither-of-which-holds-today).
- **Switching the store on is two Prow exports, not one, and the presubmit's is the one that gets
  forgotten.** `EVAL_BASELINE_STORE` is the single variable for both directions: the nightly sets
  it and appends (`objectViewer` + `objectCreator`), and the presubmit must set it too and only
  read (`objectViewer`). A presubmit that leaves it unset reads the empty checked-in directory,
  finds nothing admitted, and reports a **legitimate green** with rung 4, rung 6 and the aggregate
  all inert — the rate-based half of this design, silently absent, with no signal that it is
  missing. The absolute rungs (1, 2, 3, 5) and the correctness floor still block, so the failure
  looks like a working gate. Neither export exists yet: the presubmit sets nothing, and
  [oss-test-infra#2665](https://github.com/GoogleCloudPlatform/oss-test-infra/pull/2665) adds it to
  the nightly only. Both wait on the bucket.
- No Prow job yet appends for `hack/ci-eval-pr.sh` (job config lives in
  `GoogleCloudPlatform/oss-test-infra`). Without one, nothing ever appends and no case is ever
  admitted. It is written and open as a draft —
  [oss-test-infra#2665](https://github.com/GoogleCloudPlatform/oss-test-infra/pull/2665), see
  [The job that writes it](#the-job-that-writes-it) — held until this pull request merges and the
  bucket exists. Three things are open on it: `EVAL_REPETITIONS: 10` and `timeout: 4h` are
  starting points rather than measurements and want tuning from the first real runs; the
  `testgrid-alert-email` is a placeholder that must not merge as-is; and it still says
  `serviceAccountName: prowjob-default-sa`, which must become `eval-baseline-recorder` before it
  leaves draft — its `TODO` reads as a preference and is not one.
- A lint that a behaviour change bumped `fleet` or `verifiers`.
- The GCS listing is unbounded while the fetch is capped. The reader lists the whole prefix and
  filters afterwards, because `BaselineStore.load` does not know which key it is about to be asked
  for and `bench-gate suite` reads many cases at potentially different keys. Scoping the listing to
  the key means threading it through both, which the layout now makes worth doing but which buys
  nothing at today's volumes; see
  [Reading is capped, and says so](#reading-is-capped-and-says-so).
- The `bench/tf/fleet` drift-reconcile schedule — a drifted fixture silently changes what a
  baseline means.
- **The aggregate's sample floor is a placeholder for a real test.**
  `EVAL_AGGREGATE_MIN_SCORED` (default 30) refuses to compare below ten admitted cases' worth of
  repetitions, because a flat margin at `n = 3` measures luck. It is a floor rather than a wider
  margin because no single flat margin is right at both `n = 3` and `n = 600`, and the fix that
  actually scales is a two-proportion test — which needs a variance estimate that does not exist
  until the nightly has run against `main` enough times to produce one. Until then the aggregate is
  advisory on small runs and says so in the verdict. Two things to watch when it is replaced: `30`
  is not load-bearing except as "enough to tolerate two failed repetitions", and the advisory note
  must keep reporting when the rate fell below the margin, or a rule that never fires goes
  unnoticed.
- Every threshold here is a starting point. The way to tune them is to run the suite against `main`
  a few dozen times, see how much it moves when nothing changed, and set the bars above that.
