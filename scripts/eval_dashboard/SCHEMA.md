# data.json schema (version 1)

`collect.py` writes this file; the dashboard renderer and the publisher are
built against it **in parallel**. It is a contract: field names, types and
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
      "runs_on_record": 4,
      "pass_rate": 1.0,
      "last3": ["pass", "pass", "pass"],
      "durations": { "min": 145, "med": 165, "max": 182 },
      "ov_history": [{ "build_id": "...", "value": 1.0 }]
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

- `build_id` — the Prow build directory name, as a **string** (the ids
  overflow 53-bit JSON-consumer integers).
- `pr` — `started.json`'s `pull`, falling back to the number in the GCS
  path. `null` when neither is available.
- `head_sha` — first 7 chars of `finished.json`'s `revision`; `null` when
  absent.
- `project` — from the `Successfully leased project: <name>` log line;
  `null` when the log never got that far.
- `started` / `finished` — `started.json` / `finished.json` timestamps as
  ISO 8601 UTC; `null` when unparseable.
- `result` — `finished.json`'s `result` verbatim: `SUCCESS`, `FAILURE` or
  `ABORTED`. This is the Prow job verdict, not the eval verdict.
- `duration_s` — the `Total Duration` of the final
  `PR Smoke Test Evaluation Succeeded/Failed` line (eval loop only). A
  truncated log has no verdict line; then it falls back to
  `finished − started` (which also counts provisioning).
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

### `cases[]` — one entry per task name seen in any run, sorted by name

- `domain` — the top-level `domain:` field of
  `bench/tasks/<name>/task.yaml` **on the checkout the collector runs
  from**; `"unknown"` for a historical task with no yaml (renamed or
  deleted). Never a crash.
- `active` — `true` iff the name is an **uncommented** entry in
  `hack/ci-eval-pr.sh`'s `TASKS` array (same textual parse as
  `scripts/test_domain_coverage.py`). Historical-only cases are kept with
  `active: false`.
- `runs_on_record` — total task appearances across all runs, `infra`
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
- **Known gap:** multi-repetition verdict lines carry no task-level
  duration or OutcomeValidity, so `durations` and `ov_history` accrue only
  from single-repetition-era runs and freeze once those age out of the
  window. Collecting per-rep durations from the per-rep finish markers
  (`<<< finished <task> rep N in Ss`) is a follow-up; renderers should not
  present these two as current for repetition-era data.

### Optional top-level fields

Additive, optional, and safe to omit — consumers must default them.

- `stale_after_s` — seconds after `generated_at` beyond which the rendered
  page labels itself `STALE`. No collector version emits it yet; the
  renderer defaults to `7200` when it is absent.

### `coverage` — from `docs/designs/domains.yaml`

- `domains_total` — number of entries under `domains:`.
- `uncovered` — the `allowlist` entries (the domains known-uncovered
  today).
- `domains_covered` — `domains_total − len(uncovered)`.

## Sources

- `--pr-glob <gs glob>` (repeatable) — Prow build dirs, discovered with
  `gsutil ls`, read with `gsutil cat`. **Read-only.**
- `--from-dir <dir>` — local `<build_id>/` subdirectories with the same
  three files; the offline/testing path.

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
