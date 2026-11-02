"""Whether a cron roster job fires once a day, every day.

A failed run's chat line ends "It will try again tomorrow." only for such a
job, and "It will try again at its next scheduled run." for any other or for
one whose schedule cannot be read. No clock time: under `spec.mode: next`,
the default for a first install, the gateway escapes Slack's date token, and a
time in one zone is wrong for most readers. Callers: the chat relay
(`session_kv_server.scheduler_failure_line`) and `upgrade_readiness_watch.py`.
"""

from __future__ import annotations

import json
import os
from typing import Any

CRON_ROSTER = ("cron", "jobs.json")
CRON_KIND = "cron"
CRON_FIELD_COUNT = 5
EVERY = "*"


def is_daily(expr: object) -> bool:
    """Whether a five-field cron expression fires once a day, every day: a fixed minute and hour, the rest `*`."""
    fields = str(expr or "").split()
    if len(fields) != CRON_FIELD_COUNT:
        return False
    minute, hour, *rest = fields
    return minute.isdigit() and hour.isdigit() and all(field == EVERY for field in rest)


def job_runs_daily(profile_home: str | os.PathLike, job_id: str) -> bool:
    """Whether `job_id` in the cron roster under `profile_home` fires daily; False when it cannot be read."""
    try:
        with open(os.path.join(profile_home, *CRON_ROSTER), encoding="utf-8") as handle:
            store: Any = json.load(handle)
    except (OSError, ValueError):
        return False
    jobs = store.get("jobs") if isinstance(store, dict) else store
    for job in jobs if isinstance(jobs, list) else []:
        if isinstance(job, dict) and str(job.get("id") or "") == job_id:
            schedule = job.get("schedule")
            return isinstance(schedule, dict) and schedule.get("kind") == CRON_KIND and is_daily(schedule.get("expr"))
    return False
