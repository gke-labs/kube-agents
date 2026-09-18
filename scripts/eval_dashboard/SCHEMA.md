# Eval dashboard contracts: `data.json`, `brief.json`, the pages

`collect.py` writes `data.json`; the renderer and the publisher read it. It is a contract: field names, types and
derivation rules below are fixed. Changes must be additive optional fields
only — anything that renames, removes or re-types a field bumps
`schema_version` and lands together with both consumers.

```json
{
  "schema_version": 1,
  "generated_at": "<iso8601>",
  "source": "logs",
  "runs": [
    {
      "build_id": "2093054394793725952",
      "tier": "presubmit",
      "job": "pull-kube-agents-smoke-test",
      "pr": 998,
      "head_sha": "a28f0b3",
      "project": "kube-agents-evals-2",
      "started": "<iso8601>",
      "finished": "<iso8601>",
      "result": "SUCCESS|FAILURE|ABORTED",
      "eval_verdict": "GREEN|RED|null",
      "duration_s": 5793,
      "tasks": [
        {
          "name": "reliability-pdb-probe",
          "result": "pass|fail|infra",
          "duration_s": 182,
          "outcome_validity": 1.0
        }
      ]
    }
  ],
  "cases": [
    {
      "name": "...",
      "domain": "reliability",
      "active": true,
      "nightly_active": true,
      "runs_on_record": 4,
      "pass_rate": 1.0,
      "last3": ["pass", "pass", "pass"],
      "durations": { "min": 145, "med": 165, "max": 182 },
      "ov_history": [{ "build_id": "...", "value": 1.0 }],
      "nightly": { "runs_on_record": 1, "pass_rate": 1.0, "last3": ["pass"] }
    }
  ],
  "coverage": {
    "domains_total": 11,
    "domains_covered": 10,
    "uncovered": ["incident-triage"]
  }
}
```

## Derivation rules

### `runs[]` — one entry per finished Prow build, oldest first

Parsed from `build-log.txt` plus Prow's `started.json`/`finished.json`.
A build with no `finished.json` is still running and is skipped entirely.
One job feeds it today, the presubmit gate (`pull-kube-agents-smoke-test`,
one build per pull-request push). The collector also reads a second, the
nightly periodic (`ci-kube-agents-eval-nightly`, `EVAL_TIER=nightly` in the
same `hack/ci-eval-pr.sh`, against `main`, no pull request), which archives
the same layout and is collected from the moment it starts running.

- `build_id` — the Prow build directory name, as a **string** (the ids
  overflow 53-bit JSON-consumer integers).
- `tier` — **optional, additive**: `"presubmit"` or `"nightly"`, from the
  source the build was discovered through, never from the build's own
  metadata. **Absent means `presubmit`** — every run written before the
  field existed was one — and consumers read it through `tiers.py`'s
  `run_tier` / `presubmit_runs` / `nightly_runs`. A value outside the two
  is neither tier and counts nowhere: a run tagged some new way is never
  the gate's by default. Every gate verdict — the
  health adjudicator's rules and 24-hour metrics, `classify.py`'s "is this
  mine?" (other PRs, the only-this-PR passes, the 30-day pass rate), the
  red comment's "runs from other PRs" count (`gate_comment.py`), the
  Brief's runs list, the Grid's columns, the Cases page's strips and
  presubmit rates — reads presubmit runs only. A nightly run appears where
  the nightly is meant to: `cases[].nightly`, the Cases page's nightly rate
  columns and a case's last failure when the presubmit has none on record,
  and `classify.py`'s per-case `nightly_failed_recent` note.
- `job` — **optional, additive**: the Prow job name, read from the build
  directory's URL (the segment before the build id) or overridden by
  `--nightly-job`. `null` for a `--from-dir` build, which has no URL.
- `pr` — `started.json`'s `pull`, falling back to the number in the GCS
  path. `null` when neither is available, and **always `null` on a
  `nightly` run**: a periodic runs `main`, whatever its metadata carries.
- `head_sha` — first 7 chars of the first sha-shaped value among
  `finished.json`'s `revision` and `started.json`'s `repo-commit` (a
  periodic's `finished.json` says `revision: main` and keeps the commit in
  `started.json`); `null` when neither is one.
- `project` — from the `Successfully leased project: <name>` log line;
  `null` when the log never got that far.
- `started` / `finished` — `started.json` / `finished.json` timestamps as
  ISO 8601 UTC; `null` when unparseable.
- `result` — `finished.json`'s `result` verbatim: `SUCCESS`, `FAILURE` or
  `ABORTED`. This is the Prow job verdict, not the eval verdict.
- `eval_verdict` — **optional, additive**: the eval loop's own verdict, from
  the final `PR Smoke Test Evaluation Succeeded/Failed` line: `GREEN` or
  `RED`. `null` when the log has no such line — the job ended before its
  verdict: Prow's deadline (it delivers SIGTERM and records `FAILURE`, not
  `ABORTED`; build 2092688354838581248 below is one), a death before the
  cases, or step 0's revalidation (a `SUCCESS`). A record written before
  the field existed has no key: unknown, which is not `null`.
- `duration_s` — the `Total Duration` of the final
  `PR Smoke Test Evaluation Succeeded/Failed` line (eval loop only). A
  truncated log has no verdict line — and neither does a `SUCCESS` build that
  `hack/ci-eval-pr.sh`'s step-0 revalidation ended before the eval loop; then
  it falls back to `finished − started` (which also counts provisioning).
- `tasks[]` — one entry per `Task <name> Result:` line, in log order (a
  verdict outside the vocabulary below — only the currently-unreachable
  `[EXPECTED_FAIL]`, which no `task.yaml` sets — does not parse and yields
  no entry):
  - `result` — `pass` for `[PASSED]`, `fail` for `[FAILED]` **and**
    `[UNSTABLE]` (a multi-repetition case that passed some but not all
    graded repetitions is not a clean pass; `reps` carries the split),
    `infra` for `[RESOURCE_PREPARATION_FAILED]` (resource prep, teardown or
    agent transport failed **before grading**; the case was skipped, not
    failed).
  - `duration_s` — from `(Duration: <n>s)`; `null` if missing (always the
    case for multi-repetition logs, whose verdict lines carry no duration).
  - `outcome_validity` — from `OutcomeValidity recorded: <x>`; `null` when
    none was recorded (always the case for `infra`, and for
    multi-repetition logs).
  - `reps` — **optional, additive**: per-repetition grading detail, one
    entry per indented `rep N: <verdict> -- <text>` grading line under the
    task's verdict line, in log order:
    `{"n": <1-based int>, "result": "pass"|"fail"|"infra", "reason": <string|null>}`,
    plus `"excerpt": <string>` when the log carried one (below).
    - `result` maps the grading verdict token: `pass` → `pass`; `infra` →
      `infra`, as is any **non-pass** rep whose line carries the literal
      `KUBE_AGENTS_INFRA_FAILURE` marker; anything else (`fail`, `blocked`,
      tokens this collector has never seen) → `fail`.
    - `reason` — the free text after the first space-padded `--` separator
      (later separators belong to the reason — fail reasons contain the
      delimiter themselves), with the trailing `[OutcomeScore=…]` metrics
      dump stripped, truncated to 300 chars. `null` for passing reps and
      when nothing remains.
    - `excerpt` — **optional, additive**: the agent's own words — the text
      of the `rep N report: <text>` line `bench-gate case` prints right
      under the grading line of a repetition that did not pass (since
      2026-09-15): the first 300 characters of the agent's final report
      (`results.json`'s `output`, the "Actual Output" the judge grades),
      whitespace collapsed to single spaces, `<` dropped, an ellipsis in the
      last position where it was cut; capped at 300 again here. The
      collector consumes the line whole before any other pattern reads it,
      so text the agent wrote cannot pose as the lease line, a grading line
      or the final verdict. The key is
      **absent** — never `null` or `""` — when the log carries no such line:
      a passing rep, an empty report (a transport failure's), a report line
      for a rep the log never graded (dropped, never an entry of its own),
      and every build graded before the line existed. `classify.py`'s
      `excerpt_of` reads it into the Brief's "What the agent saw" quote, the
      run page's case card and a case's `last_failure`, always from the
      repetition whose `reason` is shown, so the quote and the check beside
      it come from the same run of the agent; nothing is quoted when that
      rep has none (when no rep carries a reason, the first rep with an
      excerpt is the one the row is about, link included). The PR gate comment's Reason line stays the grader's
      `reason` and falls back to the excerpt only for a rep that has no
      reason at all.
    - **Omission semantics:** the key is absent — never `[]` — when the log
      has no `rep N:` grading lines for the task: single-repetition-era
      builds (branches predating the multi-repetition eval of 2026-08-28;
      presubmits run branch code, so no calendar date is sharp), logs
      truncated before grading, and foreign logs. Absence means _unknown_,
      and consumers must treat a missing `reps` exactly like a missing
      field, not an empty history. Serial
      (`--- [<ts>] <task> repetition N/3`) and parallel fan-out
      (`>>> [<ts>] launching <task> rep N/3`, merged 2026-08-31) runs print
      the same grading block, so both populate `reps` identically; launch
      markers alone carry no verdict and never fabricate entries.

- `pr_merged` — **optional, additive**: `true` when the run's `pr` had
  merged at collection time, `false` when it was open or closed unmerged,
  `null` when it could not be resolved (no `pr`, `gh` failed, or the run is
  outside the resolution window below). Absent when the collector ran
  without a `gh` binary configured; consumers must treat absent and `null`
  identically (unknown). Resolved best-effort with one
  `gh pr view <pr> --repo gke-labs/kube-agents --json state,mergedAt` per
  **distinct** PR per collect invocation, and **only for runs whose build
  started within the last 14 days** — the depth the dashboard displays —
  which is what bounds the `gh` spend of one collect however large the
  archive grows. An older run keeps whatever value it already carries, or
  gets `null` without a call. Merged is terminal: a run already carrying
  `true` is never re-asked at any age. Any failure degrades to `null` with
  a single warning naming how many PRs went unresolved, never a crash, and
  a missing binary or a timed-out call stops further calls for the rest of
  the pass.

- `has_build_log`, `pod_phase`, `pod_node`, `pod_last_event` — **optional,
  additive**: how the build ended. A pod whose node went NotReady mid-run
  (twelve runs on 2026-09-11, #1478) leaves `finished.json`, `podinfo.json`
  and **no** `build-log.txt`, and lands here as a zero-task `FAILURE` of any
  duration — the same shape as a clone failure, which does have a log. So
  for a build with no build log, or one that concluded `FAILURE` with no
  tasks, the collector reads Prow's `podinfo.json` (the pod record and its
  events) as well — one extra object per such build, none for a build that
  ran — and records `pod_phase` (`status.phase`),
  `pod_node` (`spec.nodeName`) and `pod_last_event` (the `reason` of the
  newest event by `lastTimestamp`/`eventTime`/creation, upload order
  breaking ties), each `null` when the record lacks it. `has_build_log` is
  `true` for a build with a log. Without one the collector reads the log a
  second time, then lets the pod record decide: `false` when
  `podinfo.json` answered and its `sidecar` container is still `running`
  (the kubelet stopped reporting, so Prow's uploader never ran and there is
  no log anywhere); any other sidecar state means a log was uploaded —
  `terminated` on the way out, `waiting` when the clone stage failed and
  initupload wrote it — so the miss is a failed read, `has_build_log` is
  left **absent** and only the `pod_*` trio is written; a bucket that
  served neither file leaves all four absent. A
  zero-task `FAILURE` with a log but no readable `podinfo.json` carries
  `has_build_log: true` alone. Absent means unknown; consumers treat a run
  without the fields as neither a lost pod nor anything else. Runs carried
  over by `--merge-with` keep whatever they have (older documents have
  none).

- `merge_conflict` — **optional, additive**: `true` when the zero-task
  `FAILURE` was a pull request that would not merge into its base rather than
  a setup crash (#1608). One read of `clone-records.json`, for the zero-task
  `FAILURE` subset of the builds `podinfo.json` is read for, and skipped when
  that read already said no log was uploaded: `true` when a record carrying
  `pulls` has `failed: true` and a `git merge` command that both errored and
  printed `CONFLICT`. A merge that failed any other way is `false` — a full
  disk fails the merge too, and that is the pool's problem. Absent means
  unknown and reads as a setup crash, as it did before the field existed.

A truncated log yields a **partial run** (fewer tasks, fallback duration),
never an error. A task line whose name matches nothing under `bench/tasks/`
on the current checkout still parses; only its domain lookup degrades (see
below).

### `cases[]` — one entry per task name seen in any run of either tier, sorted by name

The per-case fields are the **presubmit's** record, exactly as they were
before the nightly existed; the nightly's record sits beside them under
`nightly`, never pooled in. A case only the nightly has run is on record
with `runs_on_record: 0`, `pass_rate: null`, `last3: []` on the presubmit
side.

- `domain` — the top-level `domain:` field of
  `bench/tasks/<name>/task.yaml` **on the checkout the collector runs
  from**; `"unknown"` for a historical task with no yaml (renamed or
  deleted). Never a crash.
- `active` — `true` iff the name is an entry in
  `hack/eval/presubmit-cases.txt` (the same parse, `scripts/eval_rosters.py`, as
  `scripts/test_domain_coverage.py`). Historical-only cases are kept with
  `active: false`.
- `nightly_active` — **optional, additive**: `true` iff the name is an
  entry in `hack/eval/presubmit-cases.txt` **or** `hack/eval/nightly-cases.txt`
  — the nightly matrix is the presubmit's superset (`EVAL_TIER=nightly`
  appends the second file). `active` implies `nightly_active`; the Cases page's "nightly
  only" status is `nightly_active and not active`.
- `runs_on_record` — total task appearances across presubmit runs, `infra`
  included (it is history).
- `pass_rate` — `passes / (passes + fails)`. **`infra` results are excluded
  from the denominator** — an infrastructure failure never counts against a
  case. `null` when every run on record was `infra` (nothing graded to
  rate).
- `last3` — the last ≤3 results, **newest last**, `infra` included.
- `durations` — min/median/max of `duration_s` over **graded** (non-infra)
  runs; all three `null` when there are none. Median is rounded to an int.
- `ov_history` — `{build_id, value}` per run that recorded an
  OutcomeValidity, oldest first.
- `nightly` — **optional, additive**: `{runs_on_record, pass_rate, last3}`
  over the nightly runs alone, each derived by the rule of its presubmit
  namesake above (task-level, `infra` in `runs_on_record` and `last3`,
  out of the `pass_rate` denominator; `pass_rate` `null` when nothing was
  graded). Present on every case, zeros and `null` when the nightly has
  not run it. The renderer's per-tier 7- and 30-day rates are computed
  from `runs[]` at rep level, not from this block.
- **Known gap:** multi-repetition verdict lines carry no task-level
  duration or OutcomeValidity, so `durations` and `ov_history` accrue only
  from single-repetition-era runs and freeze once those age out of the
  window. Collecting per-rep durations from the per-rep finish markers
  (`<<< finished <task> rep N in Ss`) is a follow-up; renderers should not
  present these two as current for repetition-era data.

### Optional top-level fields

Additive, optional, and safe to omit — consumers must default them.

- `stale_after_s` — seconds after `generated_at` beyond which the rendered
  page labels itself `STALE`. Emitted only when the collector is invoked
  with `--stale-after-s` (the 15-minute refresh job passes its cadence plus
  slack); the renderer defaults to `7200` when it is absent.
- `pending_builds` — builds the GCS scan listed but could not record: no
  readable `finished.json` yet (still running, or the upload failed), or an
  index pointer that could not be read this scan, so
  they are not in `runs[]` and do not raise the watermark. Entries are
  `{"build_id": "<id>", "first_seen": "<iso8601>"}`, plus `"tier": "nightly"`
  and `"log_url"` (Spyglass's page for the build directory, as for
  `runs[].log_url`) when the nightly periodic's listing named the build
  (absent: the presubmit's, as for `runs[].tier`; both are kept across
  scans), lowest
  id first; `first_seen` is when the collector first listed the build. The next
  incremental scan re-reads exactly these ids even though they sit at or
  below the watermark, and drops an entry once it is recorded or once
  `first_seen` is more than 2 days old (`PENDING_RETRY_DAYS` — a build
  unfinished that long is a pod that died without uploading). Omitted when
  empty; a malformed value is ignored with a warning, never a crash.
- `releases[]` — release-candidate eval runs, **newest first**, at most 20
  (`RC_RELEASES_MAX`). Omitted when there are none. Collected from
  `--rc-glob` / `--rc-from-dir`, which point at `post-kube-agents-eval-rc`:
  the postsubmit that runs the same `hack/ci-eval-pr.sh` against a release
  candidate's own images. **They are never in `runs[]`**, because `runs[]`
  feeds `cases[]` and a candidate is judged against main's window rather
  than added to it (`hack/ci-eval-pr.sh:1998`: "the baseline store is read,
  never written"). The renderer shows the newest 10 of them
  (`RELEASES_MAX_ROWS`), so the store holds twice what the page displays.

```json
{
  "build_id": "2097891568546484224",
  "rc_tag": "staging_2609092307_5b5ad10",
  "commit": "5b5ad10",
  "tier": "nightly",
  "verdict": "GREEN|RED|NOT RUN|null",
  "result": "SUCCESS|FAILURE|ABORTED",
  "started": "<iso8601>",
  "finished": "<iso8601>",
  "duration_s": 15006,
  "project": "kube-agents-evals-10",
  "artifacts_url": "https://oss.gprow.dev/view/gs/...",
  "pass_rate": 0.9,
  "baseline_rate": null,
  "margin": null,
  "tasks": []
}
```

- `rc_tag`, `commit`, `tier`, `verdict`, `artifacts_url` — from the banner
  `hack/ci-eval-rc.sh` prints once per run. A missing banner means the
  driver exited on one of its early guards and measured nothing; the entry
  is still emitted, because a resolver broken for a month must not read as
  a month with no releases. `rc_tag`, `tier`, `verdict`, and
  `artifacts_url` are then `null` — but `commit` is not, when Prow recorded
  a `revision`: it falls back to that ref's first 7 characters, which for a
  tag-push postsubmit is the same commit the banner would have named.
  `artifacts_url` is additionally `null` for a run outside Prow.
- `verdict` — the eval's, which is **not** the job's: the lane is advisory,
  so a `RED` candidate still leaves a `SUCCESS` in `result`. That is the job
  config's doing — it runs the driver under `|| true` — not the driver's, so
  a future config that drops the `|| true` would make the two agree without
  anything here changing. `NOT RUN` is
  the deploy-failed path — nothing was measured, so it is not a judgement
  on the candidate.
- `pass_rate` / `baseline_rate` / `margin` — fractions in `0..1` (`margin`
  may be negative), from `bench-gate suite`'s `Admitted-case pass rate:`
  line. `baseline_rate` and `margin` are `null` while the baseline store
  holds nothing at the candidate's version key, which is what makes the
  non-inferiority number advisory; the renderer labels it so.
- `tasks` — the same shape as `runs[].tasks`, parsed by the same code.
- Collection is bounded by build id, not by a watermark: the newest
  `--rc-limit` (default 20) ids are read, minus any the `--merge-with`
  prior already covers. A recorded release is final, so a carried-forward
  entry is never re-read.

### Optional run and task fields

Additive, optional, and safe to omit — consumers must default them. The
collector's derivation rules for both live under `runs[]` above; this is
what the renderer does with them.

- `runs[].pr_merged` — `true` | `false` | `null`: whether the run's PR has
  merged. No page reads it today — the merged-PR cohort it fed left with
  the two-band page — and it stays in the contract as the collector writes
  it; a consumer that reads it must treat absent and `null` alike (unknown).
- `runs[].tasks[].reps` — the task's individual repetitions, in order:
  `[{"n": 1, "result": "pass"|"fail"|"infra", "reason": "<string>"|null}]`.
  `reason` is free-form log text (renderers must escape it). `infra` reps
  are excluded from every pass-fraction denominator, exactly like `infra`
  task results. When `reps` is absent the task's single `result` stands in
  for one rep.
- `runs[].eval_verdict` — `GREEN` | `RED` | `null`: the Nightly report reads
  it; a night that is not a `SUCCESS` and carries `null` was ended before
  its verdict and is reported as truncated. Absent means unknown.
- `runs[].log_url` — nightly runs only: Spyglass's page for the build
  directory the collector listed
  (`https://oss.gprow.dev/view/gs/<bucket>/logs/<job>/<build>`), so the
  Nightly report's links follow whichever bucket the nightly logs to.
  Absent on a nightly record means it predates the field and was read from
  the bucket the nightly used before 2026-09-15, `gs://kube-agents-prow`;
  `nightly.py` links it there.
- `runs[].has_build_log`, `runs[].pod_*` — a zero-task `FAILURE` with
  `pod_last_event: "NodeNotReady"` or `has_build_log: false` is a **lost
  pod**: `classify.py` gives it its own run-level headline (`infra`, never
  the branch's), `health.py` counts it under the `lost_pods` condition and
  never as a setup death, and `gate_comment.py` leaves the one-line "run
  lost" comment on its pull request. Absent fields make none of that
  happen.
- `runs[].merge_conflict` — `true` makes the same zero-task `FAILURE` the
  branch's own: `classify.py` verdicts it `red` with a rebase as the `do`,
  and `health.py` excludes it from `setup_deaths` and `infra_reds`.
  `gate_comment.py` does not read it; no comment is left either way.
  Absent reads as a setup crash.

### `coverage` — from `docs/designs/domains.yaml`

- `domains_total` — number of entries under `domains:`.
- `uncovered` — the `allowlist` entries (the domains known-uncovered
  today).
- `domains_covered` — `domains_total − len(uncovered)`.

## Sources

- `--pr-glob <gs glob>` (repeatable) — Prow build dirs, read with
  `gsutil cat`. **Read-only.** How they are discovered depends on whether
  there is a watermark (below): a cold sweep lists the glob itself with
  `gsutil ls`, a walk of every PR directory that grows with the archive and
  passes the collector's per-call timeout (`GSUTIL_TIMEOUT_S`) at ~1700
  builds; an incremental scan lists the job's directory index instead.
- `--index-prefix <gs prefix>` — Prow's per-job directory index,
  `gs://<bucket>/pr-logs/directory/<job>/`: one `<build_id>.txt` object per
  build holding the `gs://` path of that build's directory (its
  `latest-build.txt` is ignored). One `gsutil ls` of the prefix names every
  build in seconds; the ids above the watermark (plus `pending_builds`) are
  the only pointers read, and only those builds are then read,
  `READ_WORKERS` at a time. Defaults to the index derived from each
  `--pr-glob`'s bucket and job; an empty string disables it and the glob is
  listed even with a watermark. It changes how a `--pr-glob` scan finds
  builds, not whether one happens: `--merge-with` alone still recomputes
  without touching the bucket. A listing that fails or times out is a
  `warning: gsutil ls ... failed` line and nothing new; a pointer that
  cannot be read is a `warning: gsutil cat ... failed` line and that one
  build deferred to `pending_builds`. The refresh workflow greps for either
  line and does not publish, so a stall is never republished under a fresh
  `generated_at`.
- `--nightly-prefix [<gs prefix>]` — the nightly periodic's Prow log
  prefix, `gs://<bucket>/logs/<job>/`. For a periodic that prefix **is**
  the directory index: one `<build_id>/` directory per build beside a
  `latest-build.txt` (ignored), no pointer objects, so one `gsutil ls`
  names every build and the watermark filter runs on it directly. Every
  build read through it is `tier: "nightly"`, `pr: null`, `job` the
  prefix's last segment (or `--nightly-job`). Given without a value it is
  `gs://kube-agents-evals-nightly-logs/logs/ci-kube-agents-eval-nightly/`
  (the nightly's own bucket; the presubmit and the RC lane stay on the
  cluster default `gs://kube-agents-prow`); omitted, no
  nightly scan happens (`--merge-with` alone still recomputes without
  touching the bucket). A prefix that does not list is read three ways. A
  prefix with no objects (`gsutil` says "matched no objects") is a job
  that has not run there yet — before its first night, or after its bucket
  moved while nights from the old one are on record — so that is a
  `note: nightly prefix ... did not list` line and no nightly runs this
  scan, **not** the refusal line below: the nightly is evidence beside the
  gate, and a missing night must not stop the gate's dashboard from
  publishing. Any other failure with no night on record (no nightly
  watermark) is the same note: the periodic may simply not exist yet. With
  a night on record, any other failure is a
  `warning: gsutil ls failed for ...` line — the refusal line — because a
  prefix that listed yesterday and not today is the bucket or the grant
  failing, and republishing would freeze the nightly record under a fresh
  `generated_at` with nothing said. A listing that hangs past
  `GSUTIL_TIMEOUT_S` is the refusal line either way, as any hung `gsutil`
  call is.
- `--nightly-job <name>` — the `job` recorded on nightly runs; default
  derived from the prefix.
- `--from-dir <dir>` — local `<build_id>/` subdirectories with the same
  files; the offline/testing path. Its runs are the presubmit with
  `job: null`.
- `--rc-glob <gs glob>` (repeatable) / `--rc-from-dir <dir>` — the same two
  shapes for `post-kube-agents-eval-rc`, collected into `releases[]` rather
  than `runs[]`. `--rc-limit <n>` (default 20) bounds how many builds per
  glob are read, newest first.

### Incremental collection (the output stays schema v1; it may add the optional `pending_builds` and `releases`)

- `--merge-with <data.json | gs:// URL>` — load a previously written
  data.json, carry its `runs[]` over (verbatim except `pr_merged`, which
  is re-resolved on carried runs by the same rules as on fresh ones — a
  `false`/`null` inside the 14-day window is re-asked, `true` is
  terminal), and skip every GCS build whose id is ≤ the newest
  **numeric** `build_id` on record **for that source** — the presubmit
  scan resumes above the newest presubmit run, the nightly scan above the
  newest nightly run; Prow's ids are one global sequence, so the newest
  presubmit id is normally far above every nightly id and a shared
  watermark would skip every night — except the
  ids on the prior's `pending_builds`, which are re-read regardless (the
  list is shared: an id is only ever re-read where its own source's
  listing names it). Prow
  build ids increase monotonically **by start time**, not by finish time,
  so the watermark alone would permanently skip a build that was still in
  flight when a later, shorter build got recorded; `pending_builds` (see
  Optional top-level fields) is how those builds get back in. Overlapping
  builds dedupe by `build_id` with the **freshly parsed** copy winning;
  `cases[]` and `coverage` are recomputed from the merged run list on the
  current checkout. A missing, unreadable, truncated, non-v1 or
  implausible prior file is a **warning that degrades to a fresh sweep
  bounded to `--since-days 14`** — never a crash (the first armed run has
  no prior file at all). This is what lets a 15-minute periodic republish
  in minutes instead of re-reading ~3 objects per archived build.
- `--since-days <n>` — skip GCS builds whose `started.json` timestamp is
  older than `n` days. Costs one probe read per candidate build and saves
  the other two; builds with an unreadable `started.json` are kept (the
  no-`finished.json` rule still skips them). `--from-dir` sources are
  never filtered.
- `--stale-after-s <seconds>` — write `stale_after_s` (see Optional
  top-level fields) into the output. Omitted, the field is omitted and the
  renderer's default applies.

## The rendered pages

`render.py` writes six pages beside `data.json`. Every time shown is
America/Toronto ("ET"), formatted in the browser with
`Intl.DateTimeFormat`; URL parameters stay ISO 8601 UTC.

| Page           | What it is                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `index.html`   | **The Brief**: the gate's state and why, what the agent saw, what changed right before, what is being done, the runs in the window with a "See it in the grid" link, and the last release-candidate eval runs (`releases[]`). Healthy: the last 24 hours in numbers and the last incident.                                                                                                                                                                                                                                                  |
| `run.html`     | **The PR view**, `run.html#build=<prow build id>`: one run, each failed gate case tagged `failing on N other PRs` / `only your PR` / `quota storm` / `unexplained` with its check reason, 30-day pass rate, transcript link, a link to its row on the Cases page and a one-line Do; a "what to do" box.                                                                                                                                                                                                                                     |
| `grid.html`    | **The Grid**: one row per case (blocking cases by domain, then the held-out ones, folded away when they passed everything in the window), one column per presubmit run in a window of 6 h, 24 h, 36 h or 7 days (header: PR # and ET start; a green run that recorded no cases gets no column); cells passed / failed all reps / failed some / quota-infra / died before the cases / still running (`pending_builds`); merges to main and incident starts and ends marked between the columns; a cell opens that run's detail for the case. |
| `cases.html`   | **The Cases page** ("How reliable is each test?"): one row per case by domain — its last `STRIP_RUNS` presubmit outcomes, pass rate over reps at 7 and 30 days for the presubmit and the nightly apart (`—` when a tier has no graded run), its roster status (blocking / held out / demoted with its date / nightly only / not in any matrix), its last failure with the grader's reason, and its issues from `case-notes.yaml`.                                                                                                           |
| `nightly.html` | **The Nightly report**: last night's run of the nightly tier (or the night `#build=` names) — its wall clock and whether it ran to the end, the counts (passed all reps / partial / failed / infra), what is newly failing against the night before and what passes again, every case by domain with its state, reps, the grader's reason and a transcript link, and the other nights on record. The Brief's "Last night's run" block and the 9 AM Chat digest link here. `nightly.py` derives it.                                          |
| `trend.html`   | **The Trend page** ("Scores over time on main"): the store's nightly records per case and domain — pass rate by night with the trailing admission window and the bar, judged quality by night with the spread the store supports (a case: the range of its nightly means over seven nights at one key; a domain: the range across its cases), a marker on each night the version key changed, a table under each chart, a banner when the store was not read. `#cases=`, `#domain=`, `#since=` (incident). `trend.py`, from `store.json`.   |

The pages render in the browser from `brief.json` (below),
which `render.py` inlines into each page as
`<script type="application/json" id="inline-brief">` (the verdict it read,
the same document as `brief.health`, again as `inline-health`), so a page
needs no request beyond itself (the `trend` block, which grows every night,
is inlined into `trend.html` only; the other pages and `brief.json` itself
carry `trend: null`, and the block is published as `trend.json` beside it);
the poll of the published `brief.json` and `health.json` every 60 seconds
(and of `trend.json`, on the Trend page alone) is a best-effort refresh on
top. That matters on `storage.cloud.google.com`, which answers an XHR
with a login redirect: the pages still render whole there. The header
badge says `updated <time> · Nm ago`, plus `· regenerated every 15 min`
while no poll has succeeded (the workflow republishes every page on that
cron, so that is how old the inlined copy can be); `STALE` is prepended
only when the data's `generated_at` is older than its `stale_after_s`.
`render.py --public-url [BASE]` emits `<base href>` so every relative link
resolves to the published site wherever the browser landed after the
login redirect; the bare flag means `post_health.DASHBOARD_URL`'s
directory, and without the flag links stay relative for a local render.
`classify.py` is the one place the "is this red mine?" rule lives; the
pages read its answer through `brief.json`, and anything else that answers
the question imports it.

### URL contract

`index.html#since=<ISO 8601 UTC>&until=<ISO 8601 UTC>&cases=a,b&view=gate|agent`
`run.html#build=<prow build id>`
`grid.html#since=<ISO 8601 UTC>&until=<ISO 8601 UTC>&cases=a,b[&window=6h|24h|36h|7d][&rows=all|admitted|failing]`
`cases.html#<case id>`, or `cases.html#sort=worst|domain|name&show=all|blocking|held`
`nightly.html[#build=<prow build id>]`
`trend.html#cases=a,b`, or `trend.html#domain=<domain>`, each `[&metric=<judged metric>][&since=<ISO 8601 UTC>[&until=<ISO 8601 UTC>]]`

Every parameter travels in the URL fragment as `key=value` pairs joined
by `&`. `storage.cloud.google.com` answers an unauthenticated request with
a login redirect that comes back without the query string, so a scope
carried there arrived empty and the reader landed on the unscoped Brief; a
browser never sends the fragment to the server and carries it through a
redirect, so a scope carried there survives. The older form,
`index.html?cases=a,b&since=…&until=…#gate|#agent`, `run.html?build=<id>`
and the same query form on the Grid and the Cases page, is still read, so
a link already posted to Chat, a pull request or an issue opens the same
page wherever its query survives (a session the host does not redirect, a
local render); a key present in both places is read from the query.
`linkState()` in `template/pages.js` is the one parser;
`post_health.dashboard_link` / `run_link` (Python: the Chat messages, the
gate comment, the tracking issue) and `briefHref` / `gridHref` / `runHref`
/ `caseHref` / `nightHref` (the pages' own links; `incidentHref` and
`numbersHref` wrap the first) are the writers. A writer omits an empty parameter.

- `cases`, `since`, `until` scope the Brief to that incident (a past one
  when `until` is given). `since` is matched to an incident in
  `health-history.jsonl`; without history the parameters describe it. On
  the Grid the same three make the incident the window and pin its cases
  first; the Brief's "See it in the grid" link carries them.
- `view=agent` shows the last 24 hours in numbers; `view=gate` lands on
  the "why we think" block (the page scrolls to the section after it
  renders). No parameters: the current state from `health.json`.
- Case ids match `[A-Za-z0-9][A-Za-z0-9._-]{0,79}`; the first 50
  (`maxLinkCases`) that do are read, and a link the pages write carries at
  most those 50. A value that fails its grammar is dropped and everything
  reaches the DOM escaped. On the Cases page a `#<case id>` fragment
  highlights that row; the PR view and the Grid link there.
- `since` and `until` are read with a `Z`, a space separator, or a UTC
  offset written `+02:00` or `+0200`, and converted; the pages themselves
  write `Z`, and nothing a writer emits is percent-encoded (the case-id
  grammar and the `Z` form need none).
- `run.html#build=<digits>`; an id not in `brief.json` shows a
  not-found page naming the window (`RUN_VIEW_DAYS`, 14 days).
- `nightly.html#build=<digits>` opens that night instead of the newest;
  an id not among the `nightly.nights[]` on record says so and links
  last night's.
- `window`, `rows`, `sort` and `show` are the Grid's and the Cases page's
  chips as parameters; a value outside the vocabulary is the default.
- On the Trend page `cases` scopes to those cases (the case-id grammar
  above), `domain` to a domain slug (same grammar), `metric` to a judged
  metric (`[A-Za-z][A-Za-z0-9_]{0,39}`, and one the store carries, else the
  default); `since` and `until` draw the incident's start and end as
  markers. No parameter is the overview of every domain.

### `brief.json` (written by `render.py`)

`{schema_version, generated_at, stale_after_s, run_days, rate_windows_days,
strip_runs, admitted[], health, history, merges, catches, cases{}, runs[],
pending[], releases[], nightly{}, trend{}}`. `runs[]` is the **presubmit's** last `run_days` of
`data.json`, oldest first — a nightly run is nobody's pull request and is
not listed — each carrying its identity and timing plus
`classify.classify_run(...)`: `verdict` (`red` = looks like the PR, `green`,
`infra` = the gate's), `headline`, `lede`, `matches_incident`,
`setup_death`, `storm_reps`, `do`, `cases[]` (`{case, outcome, cls,
also_failing_prs, pass_rate_30d, reason, excerpt, rep_n, do, admitted, reps,
nightly_failed_recent}`) and `health_at` (the verdict in force when it
finished, from history; `null` without history). `rep_n` is the 1-based
repetition the row is about — the one whose `reason` is shown, else the
one whose `excerpt` is (`null` when there is neither); the pages link that
repetition's transcript, rep 1's when it is `null`. `also_failing_prs` and
`pass_rate_30d` count presubmit runs only; `nightly_failed_recent` is
`true` / `false` when the newest nightly run within two days of this one
graded the case and failed / did not fail it on every repetition, `null`
when none did — evidence about `main`, shown beside the case, never a tag.

`cases{}` is, per case, `{active, nightly_active, admitted, domain, status,
demoted_on, note, issues[], rates, strip[], last_failure}`. `status` is
`blocking` (active and in `hack/eval/blocking-roster.txt`), `held_out`
(active, off the roster), `demoted` (held out, with `demoted_on` read from the hold-out
entry in `docs/eval-gate-roster.md` that says `demoted YYYY-MM-DD`),
`nightly_only`, or `retired` (in neither matrix on this checkout); an
unreadable roster reads every active case as `blocking`, over-reporting
rather than hiding. `rates` is `{presubmit: [[pass, fail], [pass, fail]],
nightly: [...]}` over graded reps for each of `rate_windows_days` (7 and
30), run-level events excluded, `null` when nothing was graded. `strip[]`
is the case's last `strip_runs` presubmit appearances, oldest first,
`{build, pr, at, state, event}` with `state` in `pass|partial|fail|infra`
and `event` true for a run-level event. `last_failure` is the newest `fail`
or `partial` appearance — the presubmit's, else the nightly's — as `{tier,
build, pr, at, state, reps, reason, excerpt, cls, also_failing_prs, event}`
(`cls` and `also_failing_prs` from the Brief's classification of that run
when it is in `runs[]`, else `null` and `0`), or `null` when there is none.

`pending[]` is `pending_builds` as `{build, first_seen}`, the Grid's "still
running" columns — the presubmit's entries only (a night in flight,
`tier: nightly`, gets no column) and only the ones first seen inside the last
`PENDING_MAX_AGE_MS` (8 hours, past the presubmit's ceiling): an older one
is a build that never finished, not one still running. `releases[]` is `data.json`'s `releases[]` newest first,
at most `RELEASES_MAX_ROWS`, each reduced to `{build, rc_tag, commit, tier,
verdict, result, started, duration_s, artifacts_url, pass_rate,
baseline_rate, margin, cases{passed, graded, infra}}` — `artifacts_url`
only when it is `https://`, else `null`; `cases` `null` when no task
parsed. `catches` is `events.yaml`'s `catches` block or `null`. `health` is
the current verdict, `history` the ticks and the incidents derived from
them, `merges` the recent first-parent commits of the checkout (`null`
when the checkout is shallow or has no git; the Brief then omits "what
changed right before" and the Grid its merge markers).

`nightly` is `{job, nights[], running[]}` from `nightly.py`: `job` the
periodic's name as the newest nightly run carries it (the default when none
is on record),
`nights[]` the last `NIGHTS_ON_RECORD` (14) nightly runs **newest first**,
each `{build, job, head_sha, project, started, finished, duration_s, result,
log_url, truncated, complete, counts{expected, recorded, passed, partial,
failed, infra, missing}, missing[], newly_failing[], fixed[],
previous_build, cases[]}`. `cases[]` is every task row the night measured,
sorted by domain then name, as `{case, domain, state, reps{pass, fail,
infra}, reason, transcript_url}` with `state` in `pass|partial|fail|infra`
by the strip's rule over the task's reps (no `reps` key: the task's result
is one rep) and `reason` the first failing rep's grader text (`null` on a
pass). `expected` counts the cases `nightly_active` on this checkout;
`missing[]` names the expected cases the night did not record.
`truncated` is a night Prow ended before its verdict: `result == "ABORTED"`
(an interrupt), or any other non-`SUCCESS` result with `eval_verdict`
`null` — the periodic's deadline arrives as SIGTERM and Prow records
`FAILURE`, so `ABORTED` alone would miss it; a record without the field
is unknown, not truncated. `complete` is neither truncated nor missing
anything. `newly_failing`
is every `fail` tonight that was not `fail` on `previous_build`, the night
before it on record (the one past the window included), `fixed` every
`fail` then that is `pass` now; both `[]` on the first night, when
`previous_build` is `null`. `log_url` and `transcript_url` point at
Spyglass under `logs/<job>/<build>`, a periodic's path, in the bucket the
run's `runs[].log_url` names (without it: `gs://kube-agents-prow`, the
bucket before 2026-09-15). The nights are
never in `runs[]`. `running[]` is the nightly's entries of `pending_builds`
first seen inside `RUNNING_MAX_AGE` (9 hours: the periodic's 8-hour budget
and Prow's time to write `finished.json`) of `generated_at`, oldest first,
each `{build, first_seen, log_url}` — a night in flight, which the Brief's
block, the report page and the digest say instead of "no night".

`trend` is `null` in the published `brief.json` (every page polls that
file every minute and only the Trend page reads the block, which grows a
night's records every night); the block is inlined into `trend.html` and
published beside `brief.json` as **`trend.json`**, which the Trend page
polls. It is `trend.py`'s block from `store.json` (below), the evidence
store's read: `{source, read_at, error, window_days, lead_days, max_objects,
truncated{case: n}, partial, warnings[], records, metrics[], default_metric,
spread_nights, bar{rate, min_runs}, keys{}, nights[], cases{}, domains{}}`.
Without a `--store` every field is empty or `null` and the page says the
store was not read; `error` set means the last read failed or was not
attempted and the rest is the read before it. The page draws the
`window_days` before `read_at`; the `lead_days` before that were read too
(store.py) and their records feed the first drawn nights' windows and
spreads and appear nowhere else (not as points, nights, key changes or in
`records`). `keys{}` maps a key id
(`<setup_id>/<judge_model>/<scoring_version>-f<fleet>-v<verifiers>`) to its
five components. `nights[]` is every night inside the drawn window the
store holds a record for, oldest first, `{id, at, build, commit, started, log_url, cases}` — `id` is
`build:<prow build id>` from the object name (or `at:<recorded_at>` for a
record without one), `started` and `log_url` the collector's when that
build is a nightly run in `data.json` (`null` otherwise); the page dates
every night by `at`, the stamp its points and markers are placed by, and
uses `build` only for the link to the report. `cases{}` is per case `{domain, points[],
key_changes[], record}` (a case recorded in the lead-in only is absent):
`points[]` oldest first, one per record inside the drawn window, `{night,
at, build, commit, key, runs, passes, blocked, infra, judged{metric:
{mean, n, spread{low, high, nights}}}, window{runs, passes, lines, full,
cut}}` — `window` is what computed admission reads at that night (the
newest whole records at the same key pooled to `bar.min_runs`; `full` when
reached; `cut` when the pool is short while `store.json`'s `older` shows
objects at that key the read left behind, older than the span or trimmed
by the cap, so the store holds records that admission pools and this read
did not reach) and `spread` the range of the case's nightly means
over the last `spread_nights` at the same key (`nights` 1 is no spread);
`key_changes[]` is `{night, at, from, to, changed[]}` for every record
inside the drawn window whose key differs from the one before; `record` is
`{state, key, runs, passes, lines, rate, bar, as_of}` with `state` in
`would-admit|would-demote|collecting|cut`, the newest window against the
bar (`cut`: the window is not knowable from this read). `domains{}` is per domain `{cases[],
points[], key_changes[]}` with `points[]` the cases pooled per night
`{night, at, runs, passes, cases, keys[], judged{metric: {mean, n, low,
high, cases}}}` (`mean` weighted by `n`, `low`/`high` the range of the
cases' means). Only nightly records exist in the store (a pull request's
run never writes it), so nothing here is a presubmit's.

### `store.json` (written by `store.py`, read by `render.py --store`)

The evidence store (`docs/designs/eval-scorer.md`, "What is stored")
as one document: `{schema_version, source, read_at, window_days,
lead_days, max_objects, listed, fetched, truncated{case: n}, older{case:
{key: n}}, partial, warnings[], error, records[]}`. `records[]` is every object read inside the last
`window_days` plus `lead_days` (90 and 14: the page draws the window and
pools the lead-in into its first nights) and under `max_objects` per case per key
(`EVAL_BASELINE_MAX_OBJECTS`, 200, the gate's own cap), each the JSON line
as written — `{case, recorded_at, commit, key{setup_id, scoring_version,
judge_model, fleet, verifiers}, runs, passes, blocked?, infra?, judged?}` —
plus `object` (its URL) and `build` (the Prow build id from the object
name, `null` when the name carries none). `truncated` says per case how
many older objects the cap left out, and `older` per case and version key
how many objects the listing showed and the read left behind, older than
the span or trimmed by the cap, which is how the Trend page tells a short
window it cannot see the bottom of (`cut`) from one that is genuinely
short. `older`'s case and key are the directories the writer filed them
under (`evidence_store._key_segments`: each component sanitised, `unkeyed`
for a record without a key, `""` for an object filed directly under its
case), not the record's own spelling; `trend.py` maps a record's key to
that path the same way before the lookup. `partial` is `null`, or
`{fetched, remaining}` when the read stopped at its deadline
(`--deadline-s`) between waves of fetches with objects left for the next
tick, which finds them absent from its `--prior` and reads them first;
`warnings[]` names each line that
would not parse (skipped, never fatal); `error` is set when the read did
not happen — the listing failed, or the workflow called `--fail-with` for a
read its `timeout` killed or its wall clock could not fit — and the
document is the prior read written back with the reason. The reader lists
the prefix once and fetches only the objects the prior `store.json`
(`--prior`, the copy `render.py` published beside `data.json`) does not
hold: objects are immutable and the store append-only, so a record once
read is final.

### `health.json` and `health-history.jsonl` (optional inputs)

`health.json` is the CI health adjudicator's verdict, published beside
`data.json` (nothing in this directory writes it); the fields read are
`state` (`GREEN|DEGRADED|OUTAGE`), `condition`
(`shared_break|storm|setup_deaths|lost_pods|fixture_drift`), `since`, `cause`, `advice`,
`failing_cases`, `tracking_issues`, `incident`, `recovering`, `stale`,
`slow`, `pool`, `generated_at`, `tick`. Any other state, or an unreadable file, means no
verdict: the Brief says no verdict is published and shows the last 24
hours in numbers and the runs, the PR view classifies from the runs alone
and shows no gate banner. Only a `GREEN` verdict reads as healthy. For
`lost_pods` the `incident` also carries `nodes` (`{node name: runs lost on
it}`) and `event` (`true` when the loss counts as a build-cluster event);
the pages give it the same 2-hour lead on the Brief's window as a storm and
a run-page banner of its own, and otherwise show the generic degraded
headline. `issue` (`{number, url}`) may carry `condition`, the one it was
filed for. `fixture_drift` (the hourly seeded-fleet scan found a fixture
role out of its designed state; docs/ci-health.md, "The seeded-fleet scan")
carries `roles`, `projects` and `drift` in its `incident` and a
`fixture_state` block beside `metrics`; the pages show it as the generic
degraded headline, and `fixture-state.json` beside `health.json` is the
scan's own document, which no page reads. `slow` is `null` or, on a `GREEN` tick, the slow-gate note
(`{since, runs, min_s, median_s, max_s, baseline_days, baseline_runs,
baseline_p50_s, baseline_p90_s, infra_reps}`, `docs/ci-health.md`, "A slow
gate"); the pages read `since`, `runs`, `median_s`, `baseline_p50_s` and
`baseline_days` for the one sentence the Brief's healthy headline adds while
it is set.

`pool` is `null` or the pool-pressure note (`docs/ci-health.md`, "A backed-up
pool"): `{since, verdict, breach_seen, measured_at}` always, plus `{day,
window_hours, p50_s, p95_s, waiting_longest_s, waiting_now, over_threshold,
threshold_p50_s, threshold_p95_s, free, total, cause, max_concurrency}` when
`verdict` is `BREACH` or `UNMEASURED`. `waiting_longest_s` is how long the
longest run has been waiting for a project right now, `0` for an empty queue
and `null` when Deck was not read; `over_threshold` is the count already past
the p95 limit. `waiting_now` is whether that wait is past the p50 limit — a
live backlog — and `null` when Deck was unread or no limit was given. A verdict
lasts a week, so every present-tense reader asks it: the alert is withheld on
`false`, `CONTROL_PLANE` drops its diagnosis on `null`, and the digest and
Brief go past tense on either.
`breach_seen` says whether the open episode has ever measured a breach, and
`since` is the episode's start except that a `BREACH` does not inherit one from
a stretch that only ever said the queue could not be read. `metrics` carries
both across a tick that read no artifact, as `pool_since` and
`pool_breach_seen`. The two
figures are never the seven-day window's — the periodic breaches on a day's row
or on runs queued past p95 right now, and the window sits back inside its own
limit after one bad day. Exactly one of `window_hours` and `day` says which
stretch they cover: the periodic's recent window when it had the runs to judge
it and went over a limit, the worst breached day otherwise. A verdict lasts a
week, so the recent window comes first — a Thursday incident evidenced by
Monday reads as a contradiction — but a compliant stretch is the same
contradiction, only newer. Both are `null` when only the live queue breached;
`over_threshold` counts those runs. A `STALE` verdict carries no numbers: the
periodic stopped publishing, and the last reading is not evidence about now.
Unlike `slow` it is set in every state, and the pages read `verdict`, `since`,
`measured_at`, `day`, `window_hours`, `p50_s`, `p95_s`, `over_threshold`,
`threshold_p50_s` and `threshold_p95_s` for one sentence on the Brief's healthy
headline and on the last-24-hours view.
`metrics.queue_wait_p50_s` is the same job's median wait over the last day, or
`null`; it is not derived from the runs. `metrics.queue_wait_read` says whether
the artifact was there at all. Nothing else answers that: `pool` is `null` for a
healthy pool and for a failed fetch alike, and `queue_wait_p50_s` is `null` on a
day with no runs. The poster needs the difference — going blind must not read as
the episode ending. `metrics.pool_since` is the open episode's start, held
across the ticks that read no artifact and so write no `pool`, and `null` once
a tick reads one and writes none, which is the episode ending.

`health-history.jsonl` is one JSON object per line, each the full
`health.json` document as published at that tick plus
`"tick": "<ISO 8601 UTC>"`, oldest first (the reader sorts anyway and
skips a malformed line). A run of non-GREEN ticks is one incident, from
its first tick's `since` to the first GREEN tick after it; that is what
the Brief's past-incident view and the PR view's "gate state at the time"
banner read. Absent, the pages show the current verdict only. Neither
file is copied into the out-dir: the adjudicator owns both.

## Fixtures

`testdata/` holds three **real** `pull-kube-agents-smoke-test` builds
(PRs 956 and 998), logs trimmed to the eval section, `started.json` /
`finished.json` verbatim:

| build               | why it is here                                       |
| ------------------- | ---------------------------------------------------- |
| 2092688354838581248 | PR 956 — deadline truncated the log, no verdict line |
| 2093030474753511424 | PR 998 — `RESOURCE_PREPARATION_FAILED` (infra) task  |
| 2093054394793725952 | PR 998 — full run, pass/fail mix, verdict line       |

These three predate the multi-repetition eval, which is exactly why they
stay: they pin the omission semantics of `reps` (no grading lines, no key).
`testdata_reps/` holds three more **real** builds from the repetition era,
same trimming, covering both launch-marker formats and every rep verdict
token observed in the wild (`pass`, `fail`, `infra`, `blocked`):

| build               | why it is here                                   |
| ------------------- | ------------------------------------------------ |
| 2094432646640701440 | PR 1057 — parallel fan-out, green, one infra rep |
| 2094467976156680192 | PR 1075 — serial markers, aborted mid-task       |
| 2094714569262895104 | PR 1089 — blocked/infra-heavy, >300-char reasons |

`testdata_lostpod/` holds two **real** builds of 2026-09-11 (#1478), the two
zero-task shapes the health adjudicator has to tell apart. `started.json` /
`finished.json` are verbatim; `podinfo.json` is trimmed to the pod's
metadata, node, phase, container states and events (the values are real);
PR 1446's build log keeps the clone header and the failing tail, and its
`clone-records.json` keeps every field the parser reads, with only the long
`git` transcripts in `output` cut short:

| build               | why it is here                                                                                                             |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| 2098383791838990336 | PR 1118 — node went NotReady 2h08m in; no build-log.txt; `has_build_log: false`                                            |
| 2098418565454499840 | PR 1446 — clone failed (merge conflict) in 0 s; log and `clone-records.json` present, last event `Started`, phase `Failed` |

`testdata_rc/` holds one **real** `post-kube-agents-eval-rc` build — the
release-candidate job, which is a postsubmit, so its `started.json` carries no
`pull` key and its log carries no PR number:

| build               | why it is here                                        |
| ------------------- | ----------------------------------------------------- |
| 2097891568546484224 | `staging_2609092307_5b5ad10` — GREEN, no baseline yet |

Captured from
`https://oss.gprow.dev/view/gs/kube-agents-prow/logs/post-kube-agents-eval-rc/2097891568546484224`
— which is also where the job name the collector globs for is verifiable, since
nothing in this repository declares it (the job lives in
`GoogleCloudPlatform/oss-test-infra`). A wrong name degrades to an empty
`releases[]` rather than an error, so check the path before changing it.

It is the fixture for `releases[]`, and it keeps both banners the driver
prints: `resolve-rc-target.sh`'s `RELEASE CANDIDATE EVAL TARGET` near the top
and `ci-eval-rc.sh`'s `RELEASE CANDIDATE EVAL` at the end. A substring match
opens the parse on the first one, so the decoy stays in the fixture.

`testdata_health/data.json.gz` is a **real** published `data.json` reduced by
`health.py --trim` (and gzip-compressed, which `health.py --data` reads by
suffix) to the runs that finished in [2026-09-01, 2026-09-09) — the last of
them on 2026-09-08 — and the fields the health adjudicator reads (`build_id`,
`pr`, `started`, `finished`, `result`, `duration_s`, `tier` when the run
carries one, and per task `name`, `result`, `reps[].result` and the first
96 characters of `reps[].reason`);
its `trimmed` key records the source and the cut. Six of its zero-task runs
carry `result: "failure"` in lowercase, as Prow wrote them on 2026-09-05 —
the one departure from the `result` vocabulary above seen in the wild, so
consumers compare it case-insensitively. `testdata_health/roster-history.json` is the
blocking roster per era over the same week, taken from the commits that
changed it (the `BOOTSTRAP_ADMITTED` line of `hack/ci-eval-pr.sh` then;
`hack/eval/blocking-roster.txt` since 2026-09-15 — `health.Roster.from_file`
reads the file and `health.Roster.from_script_text` the old line, so an era
from before the move is taken from the script at that commit). Together they are the replay fixture
`scripts/test_eval_dashboard_health.py` asserts the week's incident
timeline against. `testdata_health/lost-pods-2026-09-11.json.gz` is the same
cut of the published `data.json` for 2026-09-11 (#1478) — the day five build
nodes went NotReady — with its twenty zero-task reds re-read by
`collect.build_run` so they carry `has_build_log` and the `pod_*` trio
(`trim` keeps those fields when the source has them); the same test file
asserts it reads as `lost_pods` and not as setup deaths.
`testdata_health/slow-gate-2026-09-14.json.gz` is the same cut for
[2026-09-07 18:00Z, 2026-09-14 18:20Z) — the seven days the slow-gate rule's
baseline needs, ending on the afternoon every run was green and three hours
long (#1586); the test asserts the `slow` note from 18:00Z that day and none
over the 09-12/13 weekend.

`testdata_store/` holds four **real** objects from the evidence store's
first recording night (2026-09-17, build 2100374258805903360, commit
`b458323d`), in the store's own layout under an `evidence/` root — the case,
the setup, the judge, the `<scoring>-f<fleet>-v<verifiers>` directory and the
`<stamp>-<build>.jsonl` name — one record per object, verbatim:

| case                            | why it is here                                         |
| ------------------------------- | ------------------------------------------------------ |
| `agent-kanban-smoke`            | 3/3, `ToolInvocation` 0.67: a clean night              |
| `rca-remediation-pr`            | 2/3: a partial night with judged means below 1         |
| `cluster-agent-crashloop-debug` | 3/3 with `OutcomeValidity` 0.9: a pass that is not 1.0 |
| `upgrades-fleet-version-table`  | 3/3, `OutcomeValidity` 0.8                             |

`scripts/test_eval_dashboard_store.py` serves them through a fake `gsutil`
(`ls` of the tree, `cat` of the files) and pins the parse, the window, the
per-key cap and the incremental read; `scripts/test_eval_dashboard_trend.py`
derives the trend from them and renders the page.

`testdata_classify/incidents.json.gz` holds a published `data.json`'s runs
for two windows of the week of 2026-09-01 (PR #913's last runs on 09-04/05;
the crashloop outage of 09-07/08 with PR #608's 15-case red inside it),
trimmed to the fields `classify.py` reads, for `test_eval_dashboard_classify.py`
and the page tests.
