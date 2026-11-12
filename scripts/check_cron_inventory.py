#!/usr/bin/env python3
"""Hold the Hermes cron rosters and the scheduling job inventory together (#5626).

Every job on the two rosters is migrating off Hermes cron (#4685), and the plan
for each lives in one row of docs/designs/scheduling-on-profiles-inventory.md.
A roster change has to say where that job lives on the new path, so this check
fails:

- always, when a roster job has no inventory row, or a row names a job no roster
  has;
- on a pull request (a base ref given, or BASE_REF in the environment), when a
  job is added, removed, or has its schedule, script, no_agent or deliver changed
  and its inventory row didn't change with it.

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

# A row of an inventory table: its first cell is the job ID in backticks.
ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|")


def tracked(entry: dict) -> tuple:
    """The fields of a roster entry the inventory records the plan for."""
    schedule = entry.get("schedule")
    expr = schedule.get("expr") if isinstance(schedule, dict) else schedule
    return (expr, entry.get("script"), bool(entry.get("no_agent")), entry.get("deliver"))


def parse_roster(text: str) -> dict[str, dict]:
    data = json.loads(text)
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    return {j["id"]: j for j in jobs if isinstance(j, dict) and isinstance(j.get("id"), str)}


def read_rosters(root: Path, ref: str | None = None) -> dict[str, dict]:
    """Both rosters' jobs by ID, from the working tree or, with ref, from git."""
    jobs: dict[str, dict] = {}
    for rel in ROSTERS:
        text = _show(root, ref, rel) if ref else _read(root / rel)
        if text is not None:
            jobs.update(parse_roster(text))
    return jobs


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
    head_jobs = read_rosters(root)
    head_text = _read(root / INVENTORY) or ""
    head_rows = inventory_rows(head_text)
    problems = static_problems(head_jobs, head_rows)
    if base:
        merge_base = _git(root, "merge-base", base, "HEAD").strip()
        base_jobs = read_rosters(root, merge_base)
        base_rows = inventory_rows(_show(root, merge_base, INVENTORY) or "")
        problems += diff_problems(base_jobs, head_jobs, base_rows, head_rows)
    return problems


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
    parser.add_argument("--base", default=None, help="the ref a pull request merges into (default: origin/$BASE_REF)")
    args = parser.parse_args(argv)
    base = args.base
    if base is None and os.environ.get("BASE_REF", "").strip():
        base = "origin/" + os.environ["BASE_REF"].strip()
    problems = check(Path(args.root), base)
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    print(f"The cron rosters and {INVENTORY} agree" + (f" (and every roster change since {base} updated its row)." if base else "."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
