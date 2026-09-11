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
  Brief's runs list, the Grid's columns, the Cases page's strips and
  presubmit rates — reads presubmit runs only. A nightly run appears where
  the nightly is meant to: `cases[].nightly`, the Cases page's nightly rate
  columns and a nightly-only case's last failure, and `classify.py`'s
  per-case `nightly_failed_recent` note.
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
    `{"n": <1-based int>, "result": "pass"|"fail"|"infra", "reason": <string|null>}`.
    - `result` maps the grading verdict token: `pass` → `pass`; `infra` →
      `infra`, as is any **non-pass** rep whose line carries the literal
      `KUBE_AGENTS_INFRA_FAILURE` marker; anything else (`fail`, `blocked`,
      tokens this collector has never seen) → `fail`.
    - `reason` — the free text after the first space-padded `--` separator
      (later separators belong to the reason — fail reasons contain the
      delimiter themselves), with the trailing `[OutcomeScore=…]` metrics
      dump stripped, truncated to 300 chars. `null` for passing reps and
      when nothing remains.
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
- `active` — `true` iff the name is an **uncommented** entry in
  `hack/ci-eval-pr.sh`'s `TASKS` array (same textual parse as
  `scripts/test_domain_coverage.py`). Historical-only cases are kept with
  `active: false`.
- `nightly_active` — **optional, additive**: `true` iff the name is an
  uncommented entry in `TASKS` **or** `NIGHTLY_TASKS` — the nightly matrix
  is the presubmit's superset (`EVAL_TIER=nightly` appends the second
  array). `active` implies `nightly_active`; the Cases page's "nightly
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
  `{"build_id": "<id>", "first_seen": "<iso8601>"}`, lowest id first;
  `first_seen` is when the collector first listed the build. The next
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
  `gs://kube-agents-prow/logs/ci-kube-agents-eval-nightly/`; omitted, no
  nightly scan happens (`--merge-with` alone still recomputes without
  touching the bucket). A prefix that does not list is read by whether a
  night is already on record. With none (no nightly watermark) the
  periodic may simply not have run yet, so that is a
  `note: nightly prefix ... did not list` line and no nightly runs this
  scan, **not** the refusal line below: the nightly is evidence beside the
  gate, and a missing night must not stop the gate's dashboard from
  publishing. With a night on record it is a
  `warning: gsutil ls failed for ...` line — the refusal line — because a
  prefix that listed yesterday and not today is the bucket or the grant
  failing, and republishing would freeze the nightly record under a fresh
  `generated_at` with nothing said. A listing that hangs past
  `GSUTIL_TIMEOUT_S` is the refusal line either way, as any hung `gsutil`
  call is.
- `--nightly-job <name>` — the `job` recorded on nightly runs; default
  derived from the prefix.
- `--from-dir <dir>` — local `<build_id>/` subdirectories with the same
  three files; the offline/testing path. Its runs are the presubmit with
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

`render.py` writes four pages beside `data.json`, all rendered in the
browser from `brief.json` (below), which every page refetches together with
`health.json` every 60 seconds. Every time shown is America/Toronto ("ET"),
formatted with `Intl.DateTimeFormat`; URL parameters stay ISO 8601 UTC.

| Page         | What it is                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| ------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `index.html` | **The Brief**: the gate's state and why, what the agent saw, what changed right before, what is being done, the runs in the window with a "See it in the grid" link, and the last release-candidate eval runs (`releases[]`). Healthy: the last 24 hours in numbers and the last incident.                                                                                                                                                                                               |
| `run.html`   | **The PR view**, `run.html?build=<prow build id>`: one run, each failed gate case tagged `failing on N other PRs` / `only your PR` / `quota storm` / `unexplained` with its check reason, 30-day pass rate, transcript link, a link to its row on the Cases page and a one-line Do; a "what to do" box.                                                                                                                                                                                  |
| `grid.html`  | **The Grid**: one row per case (blocking cases by domain, then the held-out ones, folded away when they passed everything in the window), one column per presubmit run in a window of 6 h, 24 h, 36 h or 7 days (header: PR # and ET start); cells passed / failed all reps / failed some / quota-infra / died before the cases / still running (`pending_builds`); merges to main and incident starts and ends marked between the columns; a cell opens that run's detail for the case. |
| `cases.html` | **The Cases page** ("How reliable is each test?"): one row per case by domain — its last `STRIP_RUNS` presubmit outcomes, pass rate over reps at 7 and 30 days for the presubmit and the nightly apart (`—` when a tier has no graded run), its roster status (blocking / held out / demoted with its date / nightly only / not in any matrix), its last failure with the grader's reason, and its issues from `case-notes.yaml`.                                                        |

`classify.py` is the one place the "is this red mine?" rule lives; the pages
read its answer through `brief.json`, and anything else that answers the
question imports it.

### URL contract

`index.html?cases=a,b&since=<ISO 8601 UTC>&until=<ISO 8601 UTC>#gate|#agent`
`grid.html?cases=a,b&since=<ISO 8601 UTC>&until=<ISO 8601 UTC>[&window=6h|24h|36h|7d][&rows=all|admitted|failing]`
`cases.html[?sort=worst|domain|name][&show=all|blocking|held]#<case id>`

- `cases`, `since`, `until` scope the Brief to that incident (a past one
  when `until` is given). `since` is matched to an incident in
  `health-history.jsonl`; without history the parameters describe it. On
  the Grid the same three make the incident the window and pin its cases
  first; the Brief's "See it in the grid" link carries them.
- `#agent` shows the last 24 hours in numbers; `#gate` lands on the
  "why we think" block. No parameters: the current state from `health.json`.
- Case ids match `[A-Za-z0-9][A-Za-z0-9._-]{0,79}`; the first 50
  (`maxLinkCases`) that do are read, and a link the pages write carries at
  most those 50. A value that fails its grammar is dropped and everything
  reaches the DOM escaped. On the Cases page a `#<case id>` fragment
  highlights that row; the PR view and the Grid link there.
- `since` and `until` are read with a `Z`, a space separator, or a UTC
  offset written `+02:00` or `+0200`, and converted; the pages themselves
  write `Z`.
- `run.html?build=<digits>`; an id not in `brief.json` shows a
  not-found page naming the window (`RUN_VIEW_DAYS`, 14 days).
- `window`, `rows`, `sort` and `show` are the Grid's and the Cases page's
  chips as parameters; a value outside the vocabulary is the default.

### `brief.json` (written by `render.py`)

`{schema_version, generated_at, stale_after_s, run_days, rate_windows_days,
strip_runs, admitted[], health, history, merges, catches, cases{}, runs[],
pending[], releases[]}`. `runs[]` is the **presubmit's** last `run_days` of
`data.json`, oldest first — a nightly run is nobody's pull request and is
not listed — each carrying its identity and timing plus
`classify.classify_run(...)`: `verdict` (`red` = looks like the PR, `green`,
`infra` = the gate's), `headline`, `lede`, `matches_incident`,
`setup_death`, `storm_reps`, `do`, `cases[]` (`{case, outcome, cls,
also_failing_prs, pass_rate_30d, reason, excerpt, do, admitted, reps,
nightly_failed_recent}`) and `health_at` (the verdict in force when it
finished, from history; `null` without history). `also_failing_prs` and
`pass_rate_30d` count presubmit runs only; `nightly_failed_recent` is
`true` / `false` when the newest nightly run within two days of this one
graded the case and failed / did not fail it on every repetition, `null`
when none did — evidence about `main`, shown beside the case, never a tag.

`cases{}` is, per case, `{active, nightly_active, admitted, domain, status,
demoted_on, note, issues[], rates, strip[], last_failure}`. `status` is
`blocking` (active and in `BOOTSTRAP_ADMITTED`), `held_out` (active, off
the roster), `demoted` (held out, with `demoted_on` read from the hold-out
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
running" columns. `releases[]` is `data.json`'s `releases[]` newest first,
at most `RELEASES_MAX_ROWS`, each reduced to `{build, rc_tag, commit, tier,
verdict, result, started, duration_s, artifacts_url, pass_rate,
baseline_rate, margin, cases{passed, graded, infra}}` — `artifacts_url`
only when it is `https://`, else `null`; `cases` `null` when no task
parsed. `catches` is `events.yaml`'s `catches` block or `null`. `health` is
the current verdict, `history` the ticks and the incidents derived from
them, `merges` the recent first-parent commits of the checkout (`null`
when the checkout is shallow or has no git; the Brief then omits "what
changed right before" and the Grid its merge markers).

### `health.json` and `health-history.jsonl` (optional inputs)

`health.json` is the CI health adjudicator's verdict, published beside
`data.json` (nothing in this directory writes it); the fields read are
`state` (`GREEN|DEGRADED|OUTAGE`), `condition`
(`shared_break|storm|setup_deaths`), `since`, `cause`, `advice`,
`failing_cases`, `tracking_issues`, `incident`, `recovering`, `stale`,
`generated_at`, `tick`. Any other state, or an unreadable file, means no
verdict: the Brief says no verdict is published and shows the last 24
hours in numbers and the runs, the PR view classifies from the runs alone
and shows no gate banner. Only a `GREEN` verdict reads as healthy.

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
`BOOTSTRAP_ADMITTED` roster per era over the same week, taken from the
commits that changed it. Together they are the replay fixture
`scripts/test_eval_dashboard_health.py` asserts the week's incident
timeline against.

`testdata_classify/incidents.json.gz` holds a published `data.json`'s runs
for two windows of the week of 2026-09-01 (PR #913's last runs on 09-04/05;
the crashloop outage of 09-07/08 with PR #608's 15-case red inside it),
trimmed to the fields `classify.py` reads, for `test_eval_dashboard_classify.py`
and the page tests.
