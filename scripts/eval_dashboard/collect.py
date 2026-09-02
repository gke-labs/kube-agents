#!/usr/bin/env python3
"""Collect eval smoke-test runs from Prow build logs into data.json.

The presubmit (hack/ci-eval-pr.sh, run by pull-kube-agents-smoke-test) prints
one `Task <name> Result:` line per bench case and a final verdict line, and
Prow archives the whole thing as build-log.txt next to started.json and
finished.json under gs://kube-agents-prow. Nothing structured survives the
run, so this collector re-derives structure from those lines and writes the
data.json the dashboard renders.

data.json is a CONTRACT: the renderer and the publisher are built against the
exact shape documented in SCHEMA.md. Changes must be additive optional fields
only, with schema_version bumped on anything else. The two additive fields
this collector emits beyond the v1 core: `tasks[].reps` (per-repetition
grading detail, present only when the log carries `rep N:` grading lines) and
`runs[].pr_merged` (whether the run's PR had merged at collection time,
resolved best-effort through `gh`).

Sources:
  --pr-glob   gsutil glob(s) of Prow build directories (read-only; requires
              gsutil on PATH). Repeatable.
  --from-dir  a local directory whose immediate subdirectories each hold a
              build's build-log.txt / started.json / finished.json -- the
              offline path the unit tests use.

Incremental mode (what the hourly refresh job runs):
  --merge-with  a previously written data.json (local path or gs:// URL).
              Its runs are carried over verbatim, the GCS scan skips every
              build at or below the newest build id already on record, and
              cases/coverage are recomputed from the merged run list. The
              cold sweep is ~3 serial gsutil calls per archived build --
              tens of minutes over two weeks of history -- so an hourly
              job MUST ride this watermark. Prow build ids are monotonic in
              START order, not finish order, so a build still in flight
              when a later, shorter build gets recorded would sit below the
              watermark forever; the prior file's pending_builds list is
              how those get back in: every listed-but-unrecorded build
              rides it and is re-read on the next scan regardless of the
              watermark, until it finishes or PENDING_RETRY_DAYS passes.
              A missing, unreadable or implausible prior file is a warning
              that degrades to a fresh sweep bounded by --since-days
              (default 14 in that case), never a crash: the first armed
              run has no prior file at all.
  --since-days  skip GCS builds whose started.json is older than N days.
              Costs one probe read per candidate build and saves the other
              two; the watermark filter above is free and runs first.

Builds with no finished.json are still running (or never finished uploading)
and are skipped. Everything else is parsed best-effort: a truncated log
yields a partial run, an unparseable build is skipped with a warning, and a
task name with no bench/tasks/<name>/task.yaml on the current checkout (a
historical, since-renamed case) gets domain "unknown" rather than a crash.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import statistics
import subprocess
import sys
from datetime import datetime, timedelta, timezone

SCHEMA_VERSION = 1

# Cap on the free-text reason kept from a per-repetition grading line. Fail
# reasons quote whole grader checklists and run past 1000 chars; the dashboard
# needs a tooltip-sized excerpt, not the full transcript, and the cap keeps a
# pathological line from bloating data.json.
REP_REASON_MAX_CHARS = 300

# The literal hack/ci-eval-pr.sh stamps on a repetition it excluded as an
# infrastructure failure. The `infra` verdict token on the grading line is the
# primary signal; the marker is the fallback for lines that carry the literal
# under another token.
INFRA_FAILURE_MARKER = "KUBE_AGENTS_INFRA_FAILURE"

# runs[].pr_merged is resolved against this repository. The collector only
# reads this repo's Prow archive (the --pr-glob defaults in the CI scripts),
# so the repo is a constant rather than a flag.
GH_PR_REPO = "gke-labs/kube-agents"

# Ceiling on one `gh pr view`; past it that PR's pr_merged degrades to null.
GH_TIMEOUT_S = 60

# pr_merged is resolved (and re-resolved, for a run carrying false/null from
# an earlier collection) only while the run's build started within this
# window -- the depth the dashboard displays. It is what bounds the gh spend
# of one collect however large the archive grows: without it a full sweep
# would pay one gh call per distinct PR ever archived. Older runs keep the
# value they carry, or get null without a call. true is terminal and is never
# re-asked at any age.
PR_MERGED_WINDOW_DAYS = 14

# The sweep bound a degraded --merge-with falls back to when the prior file
# is missing or unusable. Two weeks matches the depth the dashboard displays;
# unbounded would mean re-reading every archived build ever.
DEGRADED_SINCE_DAYS = 14.0

# How long a listed-but-unfinished build stays on pending_builds before the
# scan stops re-reading it. Prow's job deadline caps a real run at a few
# hours, and 2 days of hourly retries also rides out a transiently unreadable
# finished.json; a build still unfinished after that is a pod that died
# without uploading, and dropping it is what keeps the retry list -- and the
# reads it costs every sweep -- bounded.
PENDING_RETRY_DAYS = 2.0

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TASKS_DIR = REPO_ROOT / "bench" / "tasks"
EVAL_SCRIPT = REPO_ROOT / "hack" / "ci-eval-pr.sh"
DOMAINS_YAML = REPO_ROOT / "docs" / "designs" / "domains.yaml"

# One line per evaluated case, printed by hack/ci-eval-pr.sh. Observed shapes:
#   Task reliability-pdb-probe Result: [PASSED] exact checks green; \
#       OutcomeValidity recorded: 1.0 (Duration: 182s)
#   Task capacity-pinned-pool-probe Result: [FAILED] \
#       VerificationCorrectness=0.0 (floor 1.0) | \
#       OutcomeValidity recorded: 0.0 (Duration: 129s)
#   Task compliance-rbac-overgrant Result: [RESOURCE_PREPARATION_FAILED] \
#       Infrastructure setup/teardown or agent transport error (Duration: 41s)
# and, since the multi-repetition eval (2026-08-28; parallel fan-out
# 2026-08-31 -- both print the same verdict and grading lines):
#   Task reliability-pdb-probe Result: [PASSED] passed all 3 repetitions
#   Task upgrades-lagging-master-probe Result: [UNSTABLE] passed 2 of 3 \
#       repetitions (not admitted, so it cannot collapse)
#   Task security-overgrant-probe Result: [FAILED] repetition 3: <reason>
_TASK_LINE = re.compile(
    r"^Task (?P<name>\S+) Result: "
    r"\[(?P<verdict>PASSED|FAILED|UNSTABLE|RESOURCE_PREPARATION_FAILED)\]"
    r"(?P<rest>.*)$"
)
# One line per graded repetition, indented under its `Task <name> Result:`
# line; serial and parallel fan-out runs print the same grading block.
# Observed verdict tokens: pass, fail, infra, blocked. The reason may itself
# contain ` -- `, so only the first separator splits verdict from reason.
#   rep 2: fail -- VerificationCorrectness=0.5 (floor 1.0) -- <check>: ... \
#       [OutcomeScore=0.5 OutcomeValidity=0.8 ToolInvocation=0.0]
# The verdict class is wider than the observed tokens on purpose: a token
# this collector has never seen must still match, so it can grade as fail
# rather than silently vanish from reps[].
_REP_LINE = re.compile(
    r"^\s+rep (?P<n>\d+): (?P<verdict>[A-Za-z0-9_-]+)(?: -- (?P<rest>.*))?$"
)
# The bracketed per-rep score dump ending most grading lines: structured
# metrics, not reason text.
_REP_SCORES_TAIL = re.compile(r"\s*\[OutcomeScore=[^\]]*\]\s*$")
_OUTCOME_VALIDITY = re.compile(r"OutcomeValidity recorded:\s*([0-9]*\.?[0-9]+)")
_DURATION = re.compile(r"\(Duration:\s*(\d+)s\)")
_LEASE = re.compile(r"Successfully leased project:\s*(\S+)")
# `=== [ts] PR Smoke Test Evaluation Succeeded (Total Duration: 383s) ===` or
# `❌ [ts] PR Smoke Test Evaluation Failed for tasks: a b (Total Duration: 5793s)`
_FINAL_VERDICT = re.compile(
    r"PR Smoke Test Evaluation (?P<verdict>Succeeded|Failed)"
    r".*\(Total Duration:\s*(?P<duration>\d+)s\)"
)

_RESULT_BY_VERDICT = {
    "PASSED": "pass",
    "FAILED": "fail",
    # A multi-repetition case that passed some but not all graded reps. Not a
    # clean pass, so it grades as fail here; reps[] carries the split, and the
    # schema's result vocabulary (pass|fail|infra) stays closed.
    "UNSTABLE": "fail",
    # Infrastructure (resource prep, teardown, agent transport) failed before
    # the case could be graded; the case is skipped, not failed.
    "RESOURCE_PREPARATION_FAILED": "infra",
}

# Per-rep verdict tokens map the same way: pass and infra verbatim, and
# everything else -- fail, blocked (an inadmissible record, e.g. an empty
# trajectory), or a token this collector has never seen -- grades as fail.
_REP_RESULT_BY_VERDICT = {"pass": "pass", "infra": "infra"}


# --------------------------------------------------------------------------
# Build-log parsing
# --------------------------------------------------------------------------


def _rep_from_match(m: re.Match) -> dict:
    """One reps[] entry from a matched grading line."""
    verdict = m.group("verdict")
    rest = m.group("rest") or ""
    result = _REP_RESULT_BY_VERDICT.get(verdict, "fail")
    if verdict != "pass" and INFRA_FAILURE_MARKER in rest:
        # Belt and braces: an infra-excluded rep carries the literal marker
        # even if the verdict token in front of it ever changes.
        result = "infra"
    reason = None
    if result != "pass":
        # The text after the first ` -- `, scores tail stripped, capped. A
        # pass needs no reason and dropping it keeps data.json lean.
        reason = _REP_SCORES_TAIL.sub("", rest).strip()[:REP_REASON_MAX_CHARS] or None
    return {"n": int(m.group("n")), "result": result, "reason": reason}


def parse_build_log(text: str) -> dict:
    """Extract the eval facts from one build-log.txt, best effort.

    Never raises on malformed content: a truncated log simply yields fewer
    tasks and no final verdict. `rep N:` grading lines attach to the `Task
    <name> Result:` line they follow; a task whose log has none (single-rep
    era, or a log truncated before grading) simply gets no `reps` key --
    absence means unknown, never fabricated.
    """
    project = None
    tasks = []
    current = None  # the task the next rep grading line belongs to
    eval_verdict = None
    eval_duration_s = None
    for line in text.splitlines():
        m = _LEASE.search(line)
        if m:
            project = m.group(1)
            continue
        m = _TASK_LINE.match(line)
        if m:
            rest = m.group("rest")
            ov = _OUTCOME_VALIDITY.search(rest)
            dur = _DURATION.search(rest)
            current = {
                "name": m.group("name"),
                "result": _RESULT_BY_VERDICT[m.group("verdict")],
                "duration_s": int(dur.group(1)) if dur else None,
                "outcome_validity": float(ov.group(1)) if ov else None,
            }
            tasks.append(current)
            continue
        if current is not None:
            m = _REP_LINE.match(line)
            if m:
                current.setdefault("reps", []).append(_rep_from_match(m))
                continue
            if line.startswith((">>>", "===", "---")):
                # A section header (a launch or grading marker, a stage
                # banner, a profile line) closes the grading block, so a
                # stray rep-shaped line deeper in the log cannot attach to a
                # task it does not belong to.
                current = None
        m = _FINAL_VERDICT.search(line)
        if m:
            eval_verdict = m.group("verdict")
            eval_duration_s = int(m.group("duration"))
            current = None  # nothing after the verdict is grading detail
    return {
        "project": project,
        "tasks": tasks,
        "eval_verdict": eval_verdict,
        "eval_duration_s": eval_duration_s,
    }


def _iso(ts) -> str | None:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def build_run(build_id: str, read, pr_hint: int | None = None) -> dict | None:
    """Assemble one `runs[]` entry.

    `read(name)` returns the text of a file in the build directory, or None.
    Returns None when the build has no parseable finished.json -- the run is
    still in flight or never finished uploading, so there is nothing final to
    record.
    """
    finished_text = read("finished.json")
    if finished_text is None:
        return None
    try:
        finished = json.loads(finished_text)
        if not isinstance(finished, dict):
            raise ValueError("finished.json is not an object")
    except ValueError as exc:
        print(f"warning: build {build_id}: bad finished.json ({exc}); skipping", file=sys.stderr)
        return None

    started = {}
    started_text = read("started.json")
    if started_text is not None:
        try:
            started = json.loads(started_text)
            if not isinstance(started, dict):
                started = {}
        except ValueError:
            started = {}

    parsed = parse_build_log(read("build-log.txt") or "")

    pr = pr_hint
    pull = started.get("pull")
    if isinstance(pull, (int, str)) and str(pull).isdigit():
        pr = int(pull)

    head_sha = None
    revision = finished.get("revision")
    if isinstance(revision, str) and revision:
        head_sha = revision[:7]

    started_ts = started.get("timestamp")
    finished_ts = finished.get("timestamp")
    # The verdict line's Total Duration covers only the eval loop; the
    # timestamp delta (which also counts provisioning) is the fallback a
    # truncated log leaves us.
    duration_s = parsed["eval_duration_s"]
    if duration_s is None and isinstance(started_ts, int) and isinstance(finished_ts, int):
        duration_s = finished_ts - started_ts

    return {
        "build_id": str(build_id),
        "pr": pr,
        "head_sha": head_sha,
        "project": parsed["project"],
        "started": _iso(started_ts),
        "finished": _iso(finished_ts),
        "result": finished.get("result"),
        "duration_s": duration_s,
        "tasks": parsed["tasks"],
    }


# --------------------------------------------------------------------------
# Repo-derived facts: task domains, active TASKS entries, domain coverage
# --------------------------------------------------------------------------


def task_domain(name: str, repo_root: pathlib.Path = REPO_ROOT) -> str:
    """The `domain:` field of bench/tasks/<name>/task.yaml, on THIS checkout.

    A task that no longer exists (historical run of a since-renamed case)
    gets "unknown" -- never a crash, whatever the name looks like.
    """
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name or ""):
        return "unknown"
    path = repo_root / "bench" / "tasks" / name / "task.yaml"
    try:
        text = path.read_text()
    except OSError:
        return "unknown"
    m = re.search(r"^domain:\s*([A-Za-z0-9_-]+)\s*$", text, re.M)
    return m.group(1) if m else "unknown"


def active_task_names(repo_root: pathlib.Path = REPO_ROOT) -> set[str]:
    """Case names the presubmit actually runs: uncommented TASKS entries.

    Same narrow textual parse as scripts/test_domain_coverage.py -- the
    script provisions clusters, so executing it to ask is not an option.
    """
    text = (repo_root / "hack" / "ci-eval-pr.sh").read_text()
    m = re.search(r"^TASKS=\(\n(.*?)^\)$", text, re.M | re.S)
    if not m:
        raise ValueError("TASKS=( ... ) array not found in hack/ci-eval-pr.sh")
    active = set()
    for line in m.group(1).splitlines():
        line = line.strip()
        if line.startswith('"') and line.endswith('"'):
            entry = re.fullmatch(r"\./tasks/([^/]+)/task\.yaml", line.strip('"'))
            if entry:
                active.add(entry.group(1))
    return active


def coverage(repo_root: pathlib.Path = REPO_ROOT) -> dict:
    """The `coverage` block, from docs/designs/domains.yaml.

    Line-scanned rather than yaml.safe_load so the collector stays
    stdlib-only; the file's shape is owned by scripts/test_domain_coverage.py
    and the unit tests here assert this parse agrees with it.
    """
    text = (repo_root / "docs" / "designs" / "domains.yaml").read_text()
    slugs = re.findall(r"^\s*-\s*slug:\s*([A-Za-z0-9_-]+)", text, re.M)
    uncovered = []
    in_allowlist = False
    for line in text.splitlines():
        if re.match(r"^allowlist:\s*(#.*)?$", line):
            in_allowlist = True
            continue
        if not in_allowlist:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = re.match(r"^\s+-\s+([A-Za-z0-9_-]+)\s*$", line)
        if m:
            uncovered.append(m.group(1))
        else:
            break  # a new top-level key ends the allowlist
    return {
        "domains_total": len(slugs),
        "domains_covered": len(slugs) - len(uncovered),
        "uncovered": uncovered,
    }


# --------------------------------------------------------------------------
# Case aggregation
# --------------------------------------------------------------------------


def build_cases(runs: list[dict], repo_root: pathlib.Path = REPO_ROOT) -> list[dict]:
    """Derive per-case history from chronologically ordered runs.

    INFRA results never count against a case: they are excluded from the
    pass_rate denominator and from the duration stats, though they do appear
    in runs_on_record and last3 (they are still history).
    """
    active = active_task_names(repo_root)
    history: dict[str, list[tuple[dict, dict]]] = {}
    for run in runs:
        for task in run["tasks"]:
            history.setdefault(task["name"], []).append((run, task))

    cases = []
    for name in sorted(history):
        entries = history[name]
        results = [task["result"] for _, task in entries]
        graded = [r for r in results if r != "infra"]
        passes = sum(1 for r in graded if r == "pass")
        durations = [
            task["duration_s"]
            for _, task in entries
            if task["result"] != "infra" and task["duration_s"] is not None
        ]
        cases.append(
            {
                "name": name,
                "domain": task_domain(name, repo_root),
                "active": name in active,
                "runs_on_record": len(entries),
                # null when every run on record was an infra failure: there
                # is nothing graded to rate.
                "pass_rate": round(passes / len(graded), 4) if graded else None,
                "last3": results[-3:],
                "durations": {
                    "min": min(durations) if durations else None,
                    "med": int(round(statistics.median(durations))) if durations else None,
                    "max": max(durations) if durations else None,
                },
                "ov_history": [
                    {"build_id": run["build_id"], "value": task["outcome_validity"]}
                    for run, task in entries
                    if task["outcome_validity"] is not None
                ],
            }
        )
    return cases


# --------------------------------------------------------------------------
# PR merged-state enrichment (runs[].pr_merged)
# --------------------------------------------------------------------------


class _GhUnavailable(Exception):
    """gh cannot be spawned, or hangs: systemic, not a per-PR failure."""


def _pr_merged_via_gh(pr: int, gh: str) -> bool | None:
    """Whether the PR has merged, per `gh pr view`; None when gh cannot say.

    A non-zero exit (auth, rate limit, deleted PR) and unparseable output
    degrade to None; a missing binary or a call that hits GH_TIMEOUT_S raises
    _GhUnavailable so the caller can stop paying for calls that cannot
    succeed (or that each cost a full timeout).
    """
    try:
        proc = subprocess.run(
            [gh, "pr", "view", str(pr), "--repo", GH_PR_REPO, "--json", "state,mergedAt"],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _GhUnavailable(str(exc)) from exc
    if proc.returncode != 0:
        return None
    try:
        info = json.loads(proc.stdout)
        return info.get("state") == "MERGED" or bool(info.get("mergedAt"))
    except (ValueError, AttributeError):
        return None


def _started_since(run: dict, cutoff: datetime) -> bool:
    try:
        return datetime.fromisoformat(run["started"]) >= cutoff
    except (KeyError, TypeError, ValueError):
        # Unknown age reads as old: the bounded path keeps whatever value the
        # run already carries rather than paying a gh call forever.
        return False


def annotate_pr_merged(runs: list[dict], gh: str, now: datetime | None = None) -> None:
    """Set run["pr_merged"] in place, best effort: true, false, or null.

    One `gh pr view` per DISTINCT PR, cached for this invocation, and only
    for runs whose build started within PR_MERGED_WINDOW_DAYS -- the depth
    the dashboard displays -- so the gh spend of one collect stays bounded
    however many builds the sweep covers. A run outside the window keeps the
    value it already carries (an earlier collection's answer) or gets null
    without a call. Merged is terminal: a run already carrying true is never
    re-asked at any age. Any gh failure leaves runs at null and the whole
    pass emits at most one warning naming how many PRs went unresolved --
    never a crash -- and the first missing-binary or timed-out call stops
    further calls, so a dead network costs one timeout, not one per PR.
    """
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=PR_MERGED_WINDOW_DAYS)
    cache: dict[int, bool | None] = {}
    unresolved: set[int] = set()
    gh_usable = True
    for run in runs:
        if run.get("pr_merged") is True:
            continue
        if not _started_since(run, cutoff):
            if "pr_merged" not in run:
                run["pr_merged"] = None
            continue
        pr = run.get("pr")
        if not isinstance(pr, int):
            run["pr_merged"] = None
            continue
        if pr not in cache:
            answer = None
            if gh_usable:
                try:
                    answer = _pr_merged_via_gh(pr, gh)
                except _GhUnavailable:
                    gh_usable = False
            if answer is None:
                unresolved.add(pr)
            cache[pr] = answer
        run["pr_merged"] = cache[pr]
    if unresolved:
        print(
            f"warning: pr_merged unresolved for {len(unresolved)} PR(s)"
            f" ({gh} failed or unavailable); left null",
            file=sys.stderr,
        )


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------


def _gsutil(args: list[str], gsutil: str = "gsutil") -> str | None:
    try:
        proc = subprocess.run(
            [gsutil, *args], capture_output=True, text=True, timeout=300, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"warning: {gsutil} {' '.join(args)}: {exc}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


_PR_IN_PATH = re.compile(r"/pull/[^/]+/(\d+)/")


def _started_at(started_text: str | None) -> datetime | None:
    """The build's start time from started.json text, or None when unknowable."""
    if started_text is None:
        return None
    try:
        started = json.loads(started_text)
        return datetime.fromtimestamp(int(started["timestamp"]), tz=timezone.utc)
    except (ValueError, TypeError, KeyError, OSError, OverflowError):
        return None


def runs_from_gcs(
    pr_globs: list[str],
    gsutil: str = "gsutil",
    after_build: int | None = None,
    since_cutoff: datetime | None = None,
    retry_builds: frozenset[str] = frozenset(),
    unfinished: set[str] | None = None,
) -> list[dict]:
    runs = []
    for glob in pr_globs:
        listing = _gsutil(["ls", glob], gsutil)
        if listing is None:
            print(f"warning: gsutil ls failed for {glob}; skipping", file=sys.stderr)
            continue
        seen = set()
        for line in listing.splitlines():
            line = line.strip()
            # `gsutil ls a/*` expands the wildcard and prints each matched
            # directory as a `gs://.../<id>/:` header over its contents;
            # `gsutil ls a/` prints plain `gs://.../<id>/` lines. Accept both.
            if line.endswith("/:"):
                line = line[:-1]
            if not line.endswith("/") or line in seen:
                continue  # latest-build.txt, per-object lines, duplicates
            seen.add(line)
            build_id = line.rstrip("/").rsplit("/", 1)[-1]
            if not build_id.isdigit():
                continue
            # The incremental watermark. Prow build ids are monotonic in
            # START order, so a build at or below the newest RECORDED id may
            # still be in flight (started earlier, outlived the build the
            # watermark came from) -- skipping on the id alone would drop it
            # from the dashboard permanently once the watermark climbs past
            # it. retry_builds carries exactly those ids (the prior file's
            # pending_builds) back through the filter; everything else at or
            # below the watermark is already on record and costs zero reads.
            if (
                after_build is not None
                and int(build_id) <= after_build
                and build_id not in retry_builds
            ):
                continue
            m = _PR_IN_PATH.search(line)
            pr_hint = int(m.group(1)) if m else None

            cache: dict[str, str | None] = {}

            def reader(name: str, base: str = line, cache: dict = cache) -> str | None:
                if name not in cache:
                    cache[name] = _gsutil(["cat", base + name], gsutil)
                return cache[name]

            if since_cutoff is not None:
                # One probe read decides whether to pay the other two. An
                # unparseable started.json keeps the build: build_run makes
                # the final call, and a build with no readable metadata is
                # skipped there anyway.
                started_at = _started_at(reader("started.json"))
                if started_at is not None and started_at < since_cutoff:
                    continue
            try:
                run = build_run(build_id, reader, pr_hint)
            except Exception as exc:  # noqa: BLE001 -- one bad build must not kill the sweep
                print(f"warning: build {build_id}: {exc}; skipping", file=sys.stderr)
                if unfinished is not None:
                    unfinished.add(build_id)
                continue
            if run is None:
                print(f"note: build {build_id}: no finished.json; skipping", file=sys.stderr)
                if unfinished is not None:
                    unfinished.add(build_id)
                continue
            runs.append(run)
    return runs


def runs_from_dir(root: pathlib.Path) -> list[dict]:
    runs = []
    for build_dir in sorted(p for p in root.iterdir() if p.is_dir()):

        def reader(name: str, base: pathlib.Path = build_dir) -> str | None:
            try:
                return (base / name).read_text()
            except OSError:
                return None

        try:
            run = build_run(build_dir.name, reader)
        except Exception as exc:  # noqa: BLE001
            print(f"warning: build {build_dir.name}: {exc}; skipping", file=sys.stderr)
            continue
        if run is not None:
            runs.append(run)
    return runs


# --------------------------------------------------------------------------
# Incremental merge (--merge-with)
# --------------------------------------------------------------------------


def _plausible_run(run) -> bool:
    """Whether a prior run carries every field the aggregation indexes.

    build_cases subscripts run["tasks"], task["name"], task["result"],
    task["duration_s"] and task["outcome_validity"]; a prior file missing any
    of them would crash mid-merge, so an implausible run distrusts the WHOLE
    prior file (it is self-written -- any anomaly means corruption).
    """
    if not isinstance(run, dict) or not isinstance(run.get("build_id"), str):
        return False
    tasks = run.get("tasks")
    if not isinstance(tasks, list):
        return False
    return all(
        isinstance(task, dict)
        and isinstance(task.get("name"), str)
        and task.get("result") in ("pass", "fail", "infra")
        and "duration_s" in task
        and "outcome_validity" in task
        for task in tasks
    )


def load_prior(source: str, gsutil: str = "gsutil") -> dict | None:
    """An existing data.json parsed whole, or None when it cannot be trusted.

    None -- never an exception -- for every failure mode: file or object
    missing (the first armed run has no prior), unreadable, truncated by a
    partial download, not schema v1, or runs that do not look like this
    collector's output. The caller degrades to a bounded fresh sweep.
    """
    if source.startswith("gs://"):
        text = _gsutil(["cat", source], gsutil)
        if text is None:
            print(
                f"warning: --merge-with {source}: gsutil cat failed (missing object"
                " or unreadable bucket); treating as a first run",
                file=sys.stderr,
            )
            return None
    else:
        try:
            text = pathlib.Path(source).read_text()
        except OSError as exc:
            print(
                f"warning: --merge-with {source}: {exc}; treating as a first run",
                file=sys.stderr,
            )
            return None
    try:
        data = json.loads(text)
    except ValueError as exc:
        print(
            f"warning: --merge-with {source}: not valid JSON ({exc});"
            " discarding the prior data",
            file=sys.stderr,
        )
        return None
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        version = data.get("schema_version") if isinstance(data, dict) else "n/a"
        print(
            f"warning: --merge-with {source}: schema_version {version!r} is not"
            f" {SCHEMA_VERSION}; discarding the prior data",
            file=sys.stderr,
        )
        return None
    runs = data.get("runs")
    if not isinstance(runs, list) or not all(_plausible_run(run) for run in runs):
        print(
            f"warning: --merge-with {source}: runs[] does not look like this"
            " collector's output; discarding the prior data",
            file=sys.stderr,
        )
        return None
    return data


def load_prior_runs(source: str, gsutil: str = "gsutil") -> list[dict] | None:
    """The runs of an existing data.json, or None when it cannot be trusted."""
    data = load_prior(source, gsutil)
    return None if data is None else data["runs"]


def pending_from_prior(data: dict) -> dict[str, str]:
    """The prior file's pending_builds as {build_id: first_seen}.

    pending_builds is a retry hint, not history: dropping it merely delays
    the listed builds until the next cold sweep re-finds them. So unlike an
    implausible runs[] -- which distrusts the whole file -- a malformed
    entry only discards this field, with a warning.
    """
    entries = data.get("pending_builds")
    if entries is None:
        return {}
    pending: dict[str, str] = {}
    valid = isinstance(entries, list)
    if valid:
        for entry in entries:
            if (
                isinstance(entry, dict)
                and isinstance(entry.get("build_id"), str)
                and entry["build_id"].isdigit()
                and isinstance(entry.get("first_seen"), str)
            ):
                pending[entry["build_id"]] = entry["first_seen"]
            else:
                valid = False
                break
    if not valid:
        print(
            "warning: prior pending_builds is malformed; ignoring it (the"
            " affected builds return on the next cold sweep)",
            file=sys.stderr,
        )
        return {}
    return pending


def _pending_expired(first_seen: str, now: datetime) -> bool:
    """Whether a pending entry is past PENDING_RETRY_DAYS (unparseable == yes)."""
    try:
        seen = datetime.fromisoformat(first_seen)
    except ValueError:
        return True
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return now - seen > timedelta(days=PENDING_RETRY_DAYS)


def newest_build_id(runs: list[dict]) -> int | None:
    """The numeric watermark the incremental GCS scan resumes above."""
    ids = [int(r["build_id"]) for r in runs if str(r.get("build_id", "")).isdigit()]
    return max(ids) if ids else None


def merge_runs(prior: list[dict], fresh: list[dict]) -> list[dict]:
    """Union by build_id, freshly parsed wins, sorted oldest first.

    Fresh wins so a re-read of an overlapping build (a --from-dir merge, or a
    watermark edge case) reflects what the source says NOW; a finished build
    never changes, so the choice only matters when the prior copy was bad.
    """
    by_id = {run["build_id"]: run for run in prior}
    for run in fresh:
        by_id[run["build_id"]] = run
    return sorted(by_id.values(), key=_run_sort_key)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _run_sort_key(run: dict):
    started = run.get("started") or ""
    try:
        build_num = int(run["build_id"])
    except (ValueError, TypeError):
        build_num = 0
    return (started, build_num)


def collect(
    pr_globs: list[str] | None = None,
    from_dir: pathlib.Path | None = None,
    repo_root: pathlib.Path = REPO_ROOT,
    gsutil: str = "gsutil",
    gh: str | None = None,
    merge_with: str | None = None,
    since_days: float | None = None,
    now: datetime | None = None,
    stale_after_s: int | None = None,
) -> dict:
    # gh=None skips pr_merged resolution entirely (runs carry no key), which
    # keeps library callers and unit tests hermetic; the CLI passes its --gh
    # default so a normal collect resolves best-effort. `now` anchors the
    # pr_merged resolution window and the pending-build/--since-days clocks
    # (tests pin it; the CLI leaves it None).
    now_dt = now or datetime.now(timezone.utc)
    prior: list[dict] = []
    retry: dict[str, str] = {}  # build_id -> first_seen, still worth re-reading
    after_build = None
    if merge_with is not None:
        prior_data = load_prior(merge_with, gsutil)
        if prior_data is not None:
            prior = prior_data["runs"]
            after_build = newest_build_id(prior)
            for build_id, first_seen in pending_from_prior(prior_data).items():
                if _pending_expired(first_seen, now_dt):
                    print(
                        f"note: build {build_id}: still unfinished after"
                        f" {PENDING_RETRY_DAYS:g} days on pending_builds;"
                        " giving up on it",
                        file=sys.stderr,
                    )
                else:
                    retry[build_id] = first_seen
        # No usable prior -- or a prior that yields no numeric watermark --
        # means the incremental scan cannot resume, and an unbounded cold
        # sweep is ~3 serial gsutil calls per archived build. Bound the
        # recovery unless the caller already did.
        if after_build is None and since_days is None:
            since_days = DEGRADED_SINCE_DAYS
            print(
                f"warning: no incremental watermark from --merge-with; bounding"
                f" the fresh sweep to the last {DEGRADED_SINCE_DAYS:g} days",
                file=sys.stderr,
            )

    since_cutoff = None
    if since_days is not None:
        since_cutoff = now_dt - timedelta(days=since_days)

    fresh: list[dict] = []
    unfinished: set[str] = set()
    if from_dir is not None:
        fresh.extend(runs_from_dir(from_dir))
    if pr_globs:
        fresh.extend(
            runs_from_gcs(
                pr_globs,
                gsutil,
                after_build=after_build,
                since_cutoff=since_cutoff,
                retry_builds=frozenset(retry),
                unfinished=unfinished,
            )
        )
    if merge_with is not None:
        print(
            f"note: merged {len(prior)} prior runs with {len(fresh)} newly"
            f" collected (GCS scan resumed above build {after_build},"
            f" retrying {len(retry)} pending)",
            file=sys.stderr,
        )
    runs = merge_runs(prior, fresh)
    if gh is not None:
        # After merge_runs so runs carried forward from --merge-with are
        # covered too: a prior false/null within PR_MERGED_WINDOW_DAYS is
        # re-asked, a prior true is terminal and never re-asked.
        annotate_pr_merged(runs, gh, now=now)

    # The next scan's retry list: every build listed but not (yet) recorded
    # -- still running, or its finished.json unreadable this sweep -- keeps
    # its original first_seen so the PENDING_RETRY_DAYS clock runs from the
    # first sighting, and anything that made it into runs[] drops off.
    recorded = {run["build_id"] for run in runs}
    pending = {b: seen for b, seen in retry.items() if b not in recorded}
    for build_id in unfinished - recorded:
        pending.setdefault(build_id, now_dt.isoformat())

    data = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "logs",
        "runs": runs,
        "cases": build_cases(runs, repo_root),
        "coverage": coverage(repo_root),
    }
    if pending:
        data["pending_builds"] = [
            {"build_id": build_id, "first_seen": pending[build_id]}
            for build_id in sorted(pending, key=int)
        ]
    if stale_after_s is not None:
        # The renderer's freshness badge trips this many seconds after
        # generated_at. The publisher sets it to its own cadence with slack,
        # so the badge means "the refresh job missed ticks", not jitter.
        data["stale_after_s"] = stale_after_s
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--pr-glob",
        action="append",
        default=[],
        metavar="GS_GLOB",
        help="gsutil glob of Prow build dirs, e.g. gs://kube-agents-prow/"
        "pr-logs/pull/gke-labs_kube-agents/*/pull-kube-agents-smoke-test/*"
        " (repeatable)",
    )
    parser.add_argument(
        "--from-dir",
        type=pathlib.Path,
        help="local directory of <build_id>/ subdirs with build-log.txt,"
        " started.json and finished.json (offline/testing source)",
    )
    parser.add_argument(
        "--merge-with",
        metavar="DATA_JSON",
        help="existing data.json (local path or gs:// URL) to merge into: its"
        " runs are kept, the GCS scan only reads builds newer than its newest"
        " build id, and cases/coverage are recomputed. Missing or corrupt"
        f" degrades to a fresh sweep bounded to --since-days"
        f" {DEGRADED_SINCE_DAYS:g}",
    )
    parser.add_argument(
        "--since-days",
        type=float,
        metavar="N",
        help="skip GCS builds whose started.json is older than N days"
        " (bounds a sweep; --from-dir sources are never filtered)",
    )
    parser.add_argument("--out", type=pathlib.Path, default=pathlib.Path("data.json"))
    parser.add_argument(
        "--repo-root",
        type=pathlib.Path,
        default=REPO_ROOT,
        help="checkout to read bench/tasks, hack/ci-eval-pr.sh and"
        " docs/designs/domains.yaml from",
    )
    parser.add_argument("--gsutil", default="gsutil", help="gsutil binary to invoke")
    parser.add_argument(
        "--gh",
        default="gh",
        help="gh binary used to resolve runs[].pr_merged, best-effort (one"
        " `gh pr view` per distinct PR; failures degrade to null). Pass an"
        " empty string to skip resolution",
    )
    parser.add_argument(
        "--stale-after-s",
        type=int,
        metavar="SECONDS",
        help="write stale_after_s into data.json: how long after generated_at"
        " the rendered page's freshness badge turns amber. Set by the refresh"
        " job to its cadence plus slack; omitted, the renderer's default"
        " applies",
    )
    args = parser.parse_args(argv)

    if not args.pr_glob and args.from_dir is None and args.merge_with is None:
        parser.error("nothing to collect: pass --pr-glob, --from-dir and/or --merge-with")

    data = collect(
        pr_globs=args.pr_glob,
        from_dir=args.from_dir,
        repo_root=args.repo_root,
        gsutil=args.gsutil,
        gh=args.gh or None,
        merge_with=args.merge_with,
        since_days=args.since_days,
        stale_after_s=args.stale_after_s,
    )
    args.out.write_text(json.dumps(data, indent=2) + "\n")
    print(
        f"wrote {args.out}: {len(data['runs'])} runs, {len(data['cases'])} cases,"
        f" {data['coverage']['domains_covered']}/{data['coverage']['domains_total']}"
        " domains covered",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
