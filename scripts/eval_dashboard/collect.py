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
only, with schema_version bumped on anything else.

Sources:
  --pr-glob   gsutil glob(s) of Prow build directories (read-only; requires
              gsutil on PATH). Repeatable.
  --from-dir  a local directory whose immediate subdirectories each hold a
              build's build-log.txt / started.json / finished.json -- the
              offline path the unit tests use.

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
from datetime import datetime, timezone

SCHEMA_VERSION = 1

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
_TASK_LINE = re.compile(
    r"^Task (?P<name>\S+) Result: "
    r"\[(?P<verdict>PASSED|FAILED|RESOURCE_PREPARATION_FAILED)\]"
    r"(?P<rest>.*)$"
)
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
    # Infrastructure (resource prep, teardown, agent transport) failed before
    # the case could be graded; the case is skipped, not failed.
    "RESOURCE_PREPARATION_FAILED": "infra",
}


# --------------------------------------------------------------------------
# Build-log parsing
# --------------------------------------------------------------------------


def parse_build_log(text: str) -> dict:
    """Extract the eval facts from one build-log.txt, best effort.

    Never raises on malformed content: a truncated log simply yields fewer
    tasks and no final verdict.
    """
    project = None
    tasks = []
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
            tasks.append(
                {
                    "name": m.group("name"),
                    "result": _RESULT_BY_VERDICT[m.group("verdict")],
                    "duration_s": int(dur.group(1)) if dur else None,
                    "outcome_validity": float(ov.group(1)) if ov else None,
                }
            )
            continue
        m = _FINAL_VERDICT.search(line)
        if m:
            eval_verdict = m.group("verdict")
            eval_duration_s = int(m.group("duration"))
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


def runs_from_gcs(pr_globs: list[str], gsutil: str = "gsutil") -> list[dict]:
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
            m = _PR_IN_PATH.search(line)
            pr_hint = int(m.group(1)) if m else None
            reader = lambda name, base=line: _gsutil(["cat", base + name], gsutil)
            try:
                run = build_run(build_id, reader, pr_hint)
            except Exception as exc:  # noqa: BLE001 -- one bad build must not kill the sweep
                print(f"warning: build {build_id}: {exc}; skipping", file=sys.stderr)
                continue
            if run is None:
                print(f"note: build {build_id}: no finished.json; skipping", file=sys.stderr)
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
) -> dict:
    runs = []
    if from_dir is not None:
        runs.extend(runs_from_dir(from_dir))
    if pr_globs:
        runs.extend(runs_from_gcs(pr_globs, gsutil))
    runs.sort(key=_run_sort_key)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "logs",
        "runs": runs,
        "cases": build_cases(runs, repo_root),
        "coverage": coverage(repo_root),
    }


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
    parser.add_argument("--out", type=pathlib.Path, default=pathlib.Path("data.json"))
    parser.add_argument(
        "--repo-root",
        type=pathlib.Path,
        default=REPO_ROOT,
        help="checkout to read bench/tasks, hack/ci-eval-pr.sh and"
        " docs/designs/domains.yaml from",
    )
    parser.add_argument("--gsutil", default="gsutil", help="gsutil binary to invoke")
    args = parser.parse_args(argv)

    if not args.pr_glob and args.from_dir is None:
        parser.error("nothing to collect: pass --pr-glob and/or --from-dir")

    data = collect(
        pr_globs=args.pr_glob,
        from_dir=args.from_dir,
        repo_root=args.repo_root,
        gsutil=args.gsutil,
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
