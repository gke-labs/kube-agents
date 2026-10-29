#!/usr/bin/env python3
"""Build gate for the words the chat relay reads in a failed scheduled run.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` with the shared
scripts on ``PYTHONPATH``, in the ``platform`` stage, where both the Hermes
scheduler and ``session_kv_server.py`` are importable and the image's cron
roster is in place. ``verify_chat_relay.py`` check 11 runs earlier, where only
the scheduler is, and proves the adapter flags each failure line; this proves
the relay then says the right thing about it. Both checks below degrade
quietly when they break, which is why they fail the build instead:

1. ``session_kv_server.scheduler_failure_line`` names the scheduler's reason
   by matching the words ``cron/scheduler.py:
   _summarize_cron_failure_for_delivery`` writes
   (``SCHEDULER_FAILURE_REASONS``). Reworded upstream, a fragment matches
   nothing and the line says "It hit an error" with a promise to retry; for
   rejected credentials that drops the sentence telling an admin to act. So
   every branch of the summarizer is driven, its line must carry the fragment
   it is expected to, and every fragment must be reached by some branch.
2. ``cron_schedule.job_runs_daily`` reads ``schedule.kind`` and
   ``schedule.expr`` from a profile's ``cron/jobs.json``. In another shape every
   job is told "at its next scheduled run" instead of "tomorrow". So the image's
   roster must hold at least one job that reads as daily, Hermes'
   ``parse_schedule`` must still write that shape for a job created at runtime,
   and a failure line for a daily roster job must end on "tomorrow".

Usage::

    cd /opt/hermes && PYTHONPATH=/opt/defaults/scripts python3 verify_scheduler_failure_line.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.getcwd())

IMAGE_ROSTER = Path("/opt/platform-template/cron/jobs.json")
PROFILE = "platform"
TITLE = "Security & RBAC Posture Audit"
DAILY_EXPR = "20 6 * * *"
# A failure the next run may clear, for the roster's ending.
RETRYABLE_ERROR = "HTTP 429 TPM quota exhausted"
# Each error the scheduler can be handed, by the branch of the summarizer it
# takes, and the fragment of `SCHEDULER_FAILURE_REASONS` its line must carry.
# None is the catch-all branch, which no fragment names.
SUMMARIZER_CASES = (
    ("Script timed out after 30s: /x.py", "failed: script timed out"),
    ("HTTP 429 Too Many Requests", "failed: provider rate limit"),
    ("You have reached your weekly usage limit", "failed: provider weekly usage limit"),
    (RETRYABLE_ERROR, "failed: provider quota limit"),
    ("Cron job 'x' idle for 600s (limit 600s)", "failed: the job itself stalled"),
    ("ReadTimeout talking to the provider", "failed: provider timeout"),
    ("403 authentication failed", "failed: provider authentication error"),
    ("RuntimeError: something else broke", None),
)

FAILURES: list[str] = []


def check(condition: object, label: str, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


TMP = Path(tempfile.mkdtemp())
os.environ["PLATFORM_AGENT_HOME"] = str(TMP)
os.environ["HERMES_HOME"] = str(TMP)
os.environ["SESSION_KV_DB_PATH"] = str(TMP / "session_kv.db")

import cron.jobs as jobs  # noqa: E402
import cron.scheduler as scheduler  # noqa: E402
import cron_schedule  # noqa: E402
import session_kv_server as skv  # noqa: E402


def check_phrases() -> None:
    """1. Every summarizer branch carries the fragment the relay matches, and every fragment is reached."""
    job = {"id": "compliance-audit", "name": TITLE}
    reasons = {words: (plain, admin) for words, plain, admin in skv.SCHEDULER_FAILURE_REASONS}
    reached = set()
    for error, words in SUMMARIZER_CASES:
        line = scheduler._summarize_cron_failure_for_delivery(job, error)
        said = skv.scheduler_failure_line(PROFILE, job["id"], TITLE, line)
        if words is None:
            check(said.reason == skv.SCHEDULER_FAILURE_ERROR, f"an unnamed error reads {skv.SCHEDULER_FAILURE_ERROR!r}", said.text)
            continue
        reached.add(words)
        check(words in line.lower(), f"the scheduler's line for {error!r} still says {words!r}", line[:120])
        plain, admin = reasons.get(words, ("", False))
        check(said.reason == plain, f"the relay says {plain!r} for it", said.text)
        if admin:
            check(said.text.endswith(skv.SCHEDULER_FAILURE_NEEDS_ADMIN), "and ends on the admin sentence", said.text)
    check(not set(reasons) - reached, "every fragment the relay matches is reached by a branch", str(sorted(set(reasons) - reached)))


def check_roster() -> None:
    """2. The shipped roster and Hermes' own schedule shape read as daily, end to end."""
    check(cron_schedule.is_daily(jobs.parse_schedule(DAILY_EXPR).get("expr")), "Hermes still stores a cron schedule's expression as `expr`")
    check(jobs.parse_schedule(DAILY_EXPR).get("kind") == cron_schedule.CRON_KIND, f"and its kind as {cron_schedule.CRON_KIND!r}")

    store = json.loads(IMAGE_ROSTER.read_text(encoding="utf-8"))
    roster = store.get("jobs") if isinstance(store, dict) else store
    ids = [str(job.get("id")) for job in roster if isinstance(job, dict)]
    daily = [job_id for job_id in ids if cron_schedule.job_runs_daily(IMAGE_ROSTER.parent.parent, job_id)]
    other = [job_id for job_id in ids if job_id not in daily]
    check(daily, "the image's roster has a job that reads as daily", f"{len(ids)} jobs, none daily")
    if not daily:
        return

    roster_copy = TMP.joinpath(skv.PROFILES_DIR, PROFILE, *cron_schedule.CRON_ROSTER)
    roster_copy.parent.mkdir(parents=True)
    shutil.copy(IMAGE_ROSTER, roster_copy)
    line = scheduler._summarize_cron_failure_for_delivery({"id": daily[0], "name": TITLE}, RETRYABLE_ERROR)
    said = skv.scheduler_failure_line(PROFILE, daily[0], TITLE, line)
    check(said.text.endswith(skv.SCHEDULER_FAILURE_NEXT_DAILY), f"a failure of daily {daily[0]!r} ends on tomorrow", said.text)
    if other:
        said = skv.scheduler_failure_line(PROFILE, other[0], TITLE, line)
        check(said.text.endswith(skv.SCHEDULER_FAILURE_NEXT_UNKNOWN), f"one of {other[0]!r} does not", said.text)


def main() -> None:
    check_phrases()
    check_roster()
    shutil.rmtree(TMP, ignore_errors=True)
    if FAILURES:
        print(f"\nverify_scheduler_failure_line: {len(FAILURES)} check(s) failed", file=sys.stderr)
        sys.exit(1)
    print("\nverify_scheduler_failure_line: all checks passed")


if __name__ == "__main__":
    main()
