#!/usr/bin/env python3
"""Hold the Hermes cron rosters and the scheduling job inventory together (#5626).

Every job on the two rosters is migrating off Hermes cron (#4685), and the plan
for each lives in one row of docs/designs/scheduling-on-profiles-inventory.md.
A roster change has to say where that job lives on the new path, so this check
fails:

- always, when a roster job has no inventory row, or a row names a job no roster
  has;
- on a pull request or in the merge queue (or locally, against upstream/main or
  origin/main, or a --base you name), when a job is added, removed, or has its
  schedule, script, no_agent or deliver changed and its inventory row didn't
  change with it. On a push to main only the first check runs, since the pull
  request already ran the second.

A job moving between the two rosters, or a change to a field the inventory doesn't
track (its prompt, say), needs no row change. Each failure names the job and the
file to edit.

Run: python3 scripts/check_cron_inventory.py [--root DIR] [--base REF]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

CHAT_ROSTER = "agents/chat/defaults/cron/jobs.json"
PLATFORM_ROSTER = "agents/platform/cron/jobs.json"
INVENTORY = "docs/designs/scheduling-on-profiles-inventory.md"
ROSTERS = (CHAT_ROSTER, PLATFORM_ROSTER)

# How the run finds the commit it compares with (as check_upgrade_table_releases.py does), so the
# workflow step stays one `make` line (tests/test_docs_check_workflow.py).
EVENT_NAME_ENV = "GITHUB_EVENT_NAME"
EVENT_PATH_ENV = "GITHUB_EVENT_PATH"
PUSH_EVENT = "push"
PULL_REQUEST_EVENT = "pull_request"
MERGE_GROUP_EVENT = "merge_group"
LOCAL_BASE_REFS = ("upstream/main", "origin/main")

# A row of an inventory table: its first cell is the job ID in backticks.
ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|")


def tracked(entry: dict) -> tuple:
    """The fields of a roster entry the inventory records the plan for: the whole
    schedule (any kind) less its display label, the script, no_agent and deliver."""
    schedule = entry.get("schedule")
    if isinstance(schedule, dict):
        schedule = json.dumps({k: v for k, v in schedule.items() if k != "display"}, sort_keys=True)
    return (schedule, entry.get("script"), bool(entry.get("no_agent")), entry.get("deliver"))


def parse_roster(text: str) -> list[dict]:
    data = json.loads(text)
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    return [j for j in jobs if isinstance(j, dict) and isinstance(j.get("id"), str)]


def read_roster_entries(root: Path, ref: str | None = None) -> list[tuple[str, dict]]:
    """Every (roster file, job) on both rosters, duplicates included."""
    entries: list[tuple[str, dict]] = []
    for rel in ROSTERS:
        text = _show(root, ref, rel) if ref else _read(root / rel)
        if text is not None:
            entries += [(rel, j) for j in parse_roster(text)]
    return entries


def read_rosters(root: Path, ref: str | None = None) -> dict[str, dict]:
    """Both rosters' jobs by ID, from the working tree or, with ref, from git."""
    return {j["id"]: j for _, j in read_roster_entries(root, ref)}


def duplicate_problems(entries: list[tuple[str, dict]]) -> list[str]:
    """A job ID listed twice, in one roster or across both: the second copy would
    otherwise hide behind the first and escape both checks."""
    seen: dict[str, str] = {}
    problems = []
    for rel, j in entries:
        if j["id"] in seen:
            problems.append(f"roster job `{j['id']}` is listed twice ({seen[j['id']]} and {rel}): give the new job its own ID")
        else:
            seen[j["id"]] = rel
    return problems


def duplicate_rows(text: str) -> list[str]:
    """A job ID in the first cell of two rows, across the inventory's tables: one
    would otherwise hide the other from both checks."""
    seen: set[str] = set()
    problems = []
    for line in text.splitlines():
        match = ROW.match(line)
        if match and match.group(1) in seen:
            problems.append(f"{INVENTORY} has two rows for `{match.group(1)}`: keep one (a job moving tables moves its row)")
        elif match:
            seen.add(match.group(1))
    return problems


def inventory_rows(text: str) -> dict[str, str]:
    """Each inventory row's text, by the job ID in its first cell."""
    rows: dict[str, str] = {}
    for line in text.splitlines():
        match = ROW.match(line)
        if match:
            rows[match.group(1)] = " ".join(line.split())
    return rows


def static_problems(jobs: dict[str, dict], rows: dict[str, str]) -> list[str]:
    problems = [f"roster job `{j}` has no row in {INVENTORY}: add one saying where it lives on profiles and dispatch"
                for j in sorted(set(jobs) - set(rows))]
    problems += [f"{INVENTORY} has a row for `{r}`, which no roster has: remove the row, or fix its name"
                 for r in sorted(set(rows) - set(jobs))]
    return problems


def diff_problems(base_jobs: dict, head_jobs: dict, base_rows: dict, head_rows: dict) -> list[str]:
    problems = []
    for job_id in sorted(set(base_jobs) | set(head_jobs)):
        before, after = base_jobs.get(job_id), head_jobs.get(job_id)
        if before is not None and after is not None and tracked(before) == tracked(after):
            continue
        if base_rows.get(job_id) != head_rows.get(job_id):
            continue
        what = "was added" if before is None else "was removed" if after is None else "changed its schedule, script, no_agent or deliver"
        problems.append(f"roster job `{job_id}` {what}, but its row in {INVENTORY} didn't change: update the row (#5626)")
    return problems


def check(root: Path, base: str | None) -> list[str]:
    entries = read_roster_entries(root)
    if not entries:
        return [f"no roster job found in {' or '.join(ROSTERS)}: the rosters are missing or empty"]
    head_jobs = read_rosters(root)
    head_text = _read(root / INVENTORY) or ""
    head_rows = inventory_rows(head_text)
    problems = duplicate_problems(entries) + duplicate_rows(head_text) + static_problems(head_jobs, head_rows)
    if base:
        try:
            merge_base = _git(root, "merge-base", base, "HEAD").strip()
        except subprocess.CalledProcessError:
            return problems + [f"can't find where this branch left {base}: fetch it (git fetch origin <base>) and run again"]
        base_jobs = read_rosters(root, merge_base)
        base_rows = inventory_rows(_show(root, merge_base, INVENTORY) or "")
        problems += diff_problems(base_jobs, head_jobs, base_rows, head_rows)
    return problems


def comparison_base(root: Path, env) -> str | None:
    """The commit this run compares with, or None for the static check alone."""
    event = env.get(EVENT_NAME_ENV, "")
    if event == PUSH_EVENT:
        return None
    if event in (PULL_REQUEST_EVENT, MERGE_GROUP_EVENT):
        # A pull request runs on its merge commit, whose first parent is the base it merges into.
        if event == PULL_REQUEST_EVENT and len(_git(root, "rev-list", "--parents", "-n", "1", "HEAD").split()) > 2:
            return _git(root, "rev-parse", "HEAD^1").strip()
        payload = json.loads(Path(env.get(EVENT_PATH_ENV, "")).read_text(encoding="utf-8"))
        if event == MERGE_GROUP_EVENT:
            return payload["merge_group"]["base_sha"]
        return payload["pull_request"]["base"]["sha"]
    for ref in LOCAL_BASE_REFS:
        if subprocess.run(["git", "rev-parse", "--verify", "-q", ref], cwd=root, capture_output=True).returncode == 0:
            return _git(root, "merge-base", "HEAD", ref).strip()
    return None


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def _show(root: Path, ref: str, rel: str) -> str | None:
    result = subprocess.run(["git", "show", f"{ref}:{rel}"], cwd=root, capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--base", default=None,
                        help="the ref to compare with (default: the GitHub event's base, else upstream/main or origin/main)")
    args = parser.parse_args(argv)
    root = Path(args.root)
    base = args.base if args.base is not None else comparison_base(root, os.environ)
    problems = check(root, base)
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    print(f"The cron rosters and {INVENTORY} agree" + (f" (and every roster change since {base} updated its row)." if base else "."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
