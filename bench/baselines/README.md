# Eval baselines

Screening evidence for how each case behaves on `main`. Three of the
presubmit's rules read it: collapse (rung 4), which may only red a case that
has proved it passes reliably; judged regression (rung 6), which compares this
pull request's judge scores against main's at the same version key; and the
suite aggregate, which compares pass rates.

**This store ships empty, and it fills itself.** Every nightly run on `main`
appends what it measured (`bench-gate record`), and a case is admitted once its
accumulated evidence clears the bar. Until then nothing is admitted: rung 4
cannot fire, rung 6 has nothing to compare against, and the aggregate is
advisory. That is a legitimate green, not a broken gate — it is the gate
collecting. `BOOTSTRAP_ADMITTED` in `hack/ci-eval-pr.sh` names the cases that
keep blocking meanwhile.

## Layout

| File              | What it is                                                |
| ----------------- | --------------------------------------------------------- |
| `VERSIONS.json`   | The two hand-declared halves of the version key           |
| `<case-id>.jsonl` | One file per case, named for its `bench/tasks/` directory |

Each line of `<case-id>.jsonl` is **one batch of runs** — a deliberate 20-run
screening campaign, or the ten repetitions an ordinary nightly produced —
filed under the version key it was measured at. Newlines are shown here for the
page's sake; in the file a record is one line.

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
  "judged": { "OutcomeValidity": { "mean": 0.81, "n": 20 } }
}
```

`runs` counts only the repetitions that produced a pass or a fail. Repetitions
that were blocked by rungs 1–3, or that never produced a record at all, are
counted separately as `blocked` and `infra`, and both keys are omitted when
zero. They stay out of the rate because rungs 1–3 block absolutely whether or
not a case is admitted, so admission need not model them; they stay _in the
line_ because dropping them would make a case that crashes half the time look
perfectly reliable in its own history.

`judged` carries a mean and its own `n` per metric, so a batch of 20 outweighs
a batch of 3 when the two are pooled. A metric the run did not produce is
absent, never zero.

**The file is append-only, and that is the point.** Nothing here is ever
rewritten: a re-screen adds a line and every earlier line stays. So the file is
the case's history rather than its current state, which buys three things a
rewritten blob does not. Re-screening after a model bump is a one-line diff a
reviewer can read. The old numbers stay available to answer "did this case get
less reliable, or was it always like this" — the question that decides whether
a case is worth keeping. And two appends conflict far less often than two
rewrites of the same object, which is what lets a checked-in store survive
more than a handful of cases.

**Reading is bottom-up and cumulative.** The bar wants 20 runs and one job run
is a handful of repetitions, so a rule that read only the newest line could
never admit anything the routine job produces — the store would ship empty and
stay empty. Instead the reader walks the lines at the current key newest-first
and pools them until it holds 20 runs. One 20-run campaign is therefore one
line, two ordinary nightlies at 10 repetitions are two, and both admit.

Whole lines only: pooling overshoots to 21 rather than trimming a line to land
on 20 exactly, because trimming would invent a sub-record nobody measured.

Stopping at the bar rather than reading the whole file is what buys recency for
free. A case that starts failing has its old passing lines pushed out of the
window by the new failing ones, and **de-admits itself** — nobody edits the
store, and no line is ever deleted.

Recording is unconditional on the verdict. A red run on `main` is exactly the
evidence that de-admits a case that has stopped working; a store that recorded
only good days would drift its own bar upward until nothing could clear it and
nothing could fall back below it.

Only runs on `main` append. A pull request's own run is graded against this
store and never writes to it, so a case cannot move the baseline it is about to
be judged against. That is enforced twice — the `JOB_TYPE` condition in
`hack/ci-eval-pr.sh`, and a refusal inside `bench-gate record` itself when
`PULL_NUMBER` is set — because a guard living only in shell is one careless
edit away from being gone.

A release-candidate run does not append either, and it is the case the sentence
above does not cover: the candidate is a commit on `main`, and the run measuring
it is a periodic with no `PULL_NUMBER`, so it satisfies both conditions exactly.
`RC_COMMIT_SHA` being set is the third condition, enforced in the same two
places. It has to be, because the mistake is not correctable afterwards:
`VersionKey` names the setup, the scoring version, the judge and the two content
versions, and nothing about which build produced a sample, so a candidate's
record and `main`'s are the same record once written — and the candidate would
then be judged non-inferior to a window it had just moved.

A store that will not parse is an **error**, never an empty store. Empty means
"nothing admitted, aggregate advisory", which is a green; a corrupt file
reaching that state would silently disarm the gate.

A leftover `<case-id>.json` from the pre-JSONL format is an **error**, not a
file to skip: skipping it would read as "never screened" and silently de-admit
the case rather than telling anyone the format changed.

## Where the store lives

Two backends, one record format, identical semantics. `EVAL_BASELINE_STORE`
(or `--baseline-store`) selects one; unset, everything above happens in this
directory.

| Value             | Backend | Layout                                                  |
| ----------------- | ------- | ------------------------------------------------------- |
| unset, or a path  | Local   | `<dir>/<case-id>.jsonl`, appended to in place           |
| `gs://bucket/pfx` | GCS     | `<pfx>/<case-id>/<key…>/<recorded_at>-<build-id>.jsonl` |

The local backend is the default and stays the default: the store travels with
the checkout, so running the gate needs no credential and no network, and every
unit test is hermetic.

GCS is the intended production home for one reason — on the local backend
something has to _commit_ the file, and the CI job that measures the
evidence has no push credential. On GCS each batch is a new immutable object,
never an append to an existing one, because the grant this is built for is
`roles/storage.objectCreator`: create yes, overwrite and delete no. Append-only
becomes an IAM guarantee rather than a convention, which is stronger than git,
where a force-push can rewrite history. Object names begin with an ISO-8601 UTC
stamp so a lexical sort is chronological.

Reading needs a second role. The backend lists and `cat`s, which is
`storage.objects.list` and `storage.objects.get` — `roles/storage.objectViewer`,
which `objectCreator` does not include. The recorder needs both; a presubmit
needs only `objectViewer`, because a pull request is graded against the store
and never writes to it. Neither role carries `storage.objects.delete`.

`<key…>` is the record's own version key spelled out as directories —
`<setup-id>/<judge-model>/<sv>-f<n>-v<n>` — so an object files itself under the
software it was measured on:

```
gs://kube-agents-evals-bench/evidence/agent-kanban-smoke/
  gemini-3-1-pro-preview-kubeagents-mcp/gemini-3.1-pro-preview/v1-f1-v1/
    2026-08-01T02-03-04Z-12345.jsonl
```

Evidence is only ever pooled within one key, so filing by key means a prefix
stops growing the moment the key changes: a model bump freezes the old directory
and starts a new one. It also makes the store navigable — listing a case shows
which setups have been screened. **The path is an index, never the truth**: the
reader filters on the `key` inside each record, so a name that disagrees with
its contents loses.

`VERSIONS.json` stays here in git either way. `--baseline-dir` still points at
this directory even when evidence has moved to GCS, and the split is
deliberate: `fleet` and `verifiers` are hand-declared, reviewed configuration,
not measured data, and configuration belongs where it gets reviewed.

A GCS read pulls at most `EVAL_BASELINE_MAX_OBJECTS` (default 200, roughly 600
runs) of the newest objects **per case per key**. Per key, not per case: capping
a case as a whole would sort its key directories against each other and could
drop the current key's evidence to keep a superseded key's, silently de-admitting
the case. It never binds at realistic history depths; when it does, the verdict
says which case was capped and by how much, because a silent cap reads as "I
considered everything" when it did not.

A store that cannot be **reached** — no `gcloud`, a timeout, a 403, a 503 —
degrades to advisory with a banner in the verdict, rather than redding the job.
Nothing is admitted, so collapse and rung 6 do not evaluate and the aggregate
means nothing. That is a real loosening of the gate during an outage, and it is
still the right way round: a blip that reds every pull request is the failure
mode that gets a gate switched off. Bytes that arrived and will not parse are a
different thing and remain fatal.

Full rationale, including the approaches that were rejected:
`docs/designs/eval-scorer.md`.

## The version key

Three of the five components are read off the run rather than declared, so
they cannot go stale:

| Component         | Read from                      | Covers                                       |
| ----------------- | ------------------------------ | -------------------------------------------- |
| `setup_id`        | `manifest.json` → `setupId`    | Agent model, harness, augmentation           |
| `scoring_version` | `rows.json` → `scoringVersion` | devops-bench's roll-up formula               |
| `judge_model`     | `$JUDGE_MODEL`                 | The judge, pinned independently of the agent |
| `fleet`           | `VERSIONS.json`                | The `bench/tf/fleet` state a task audits     |
| `verifiers`       | `VERSIONS.json`                | `kube_agents_bench/verifiers.py` behaviour   |

The judge model is a component of its own, and is pinned independently of the
agent model, because a judge that tracks whatever the agent is running cannot
be told apart from an agent that got better — and a drifting judge moves every
baseline at once.

`fleet` and `verifiers` are hand-bumped integers rather than content hashes: a
hash changes on a comment typo, and re-baselining here costs a pull request
rather than a backfill. It is the same contract `bench/pyproject.toml` already
asks for the devops-bench SHA. The trade-off is real — a behaviour change with
no bump silently compares against a stale baseline — and a lint for it is
still owed.

## Admission

A case is admitted when its pooled evidence at the **current** key holds at
least `EVAL_ADMISSION_MIN_RUNS` runs (default 20) at a rate of at least
`EVAL_ADMISSION_RATE` (default 0.95).

Short of that the gate says so in the case's own words, and the four states are
distinct on purpose:

| State                   | What the presubmit prints                           |
| ----------------------- | --------------------------------------------------- |
| Nothing at this key     | `no screening evidence for this case yet`           |
| Evidence at an old key  | `stale: …`, never compared against                  |
| Fewer than the min runs | `collecting: 9/9 runs recorded … 11 more needed`    |
| At the bar, below rate  | `screened at 17/21 …, below the bar of 95% over 20` |

Only the last is a problem with the case. The middle two are the store filling
up, which is the ordinary state of a new case and of every case after a version
bump.

Admission is computed here, never declared in `task.yaml`. A pull request
author therefore cannot self-admit a case in the same diff that makes it pass.

## What invalidates a record

Anything that changes the key: a new agent or judge model, a devops-bench SHA
bump that moves `scoringVersion` or `setupId`, a `fleet` or `verifiers` bump.
The record stays in the file — it is still true about the software it was
measured on — and a new one is appended once re-screened.

## Regenerating

Ordinarily nobody does: the nightly appends a line every time it runs on
`main`, and 20 runs of evidence arrive after two nights. To fill the store
faster — a new case, or every case after a version bump — run the suite N times
on a `main` checkout and record each one:

```sh
uv run bench-gate record \
  --case-result "$ARTIFACTS"/case-*.json \
  --commit "$(git rev-parse HEAD)" \
  --lines-out /tmp/appended.jsonl
```

It refuses to run with `PULL_NUMBER` or `RC_COMMIT_SHA` set; `--force` overrides
both, for tests and local screening. It is also unconditional on the verdict,
deliberately — see above.

With `EVAL_BASELINE_STORE` pointing at a bucket the append lands and the loop
closes. Against the default local store it does not: the store lives in git,
the recorder has no push credential, and the append dies with the workspace.
`--lines-out` writes the same lines as a Prow artefact for somebody to land by
hand, which is a stopgap for the interval before the bucket exists.

Whoever writes a line — the recorder or a person — the operation is an append, and
the review question is the same: does this one new line say what the run
found? Never edit or drop a line that is already there. If a past record is
wrong rather than merely old, correct it in a commit that says so, because it
is the only way the history stops meaning what it says.
