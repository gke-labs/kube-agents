#!/usr/bin/env python3
"""Deterministic (no-LLM) delivery for first-time onboarding.

This script backs the ``bootstrap-inventory-delivery`` cron job, which runs
with ``no_agent: true``. Its stdout is delivered verbatim by the cron
scheduler to the job's configured target (``deliver: origin`` — the chat the
user first spoke in, bound by the ``bootstrap_onboarding`` plugin).

Delivery happens exactly once, and only when discovery has finished AND a
human has connected:

- ``INVENTORY.md`` present  -> the background scan has produced the report.
- ``.user_aligned`` present -> a human has opened the chat (set by the plugin;
  never by a background task — see the plugin README).
- ``.bootstrap_completed`` absent -> the report has not been delivered yet.

When all three hold, the script prints ``INVENTORY.md`` (delivered verbatim),
marks onboarding complete, removes the single-use report, and removes the two
onboarding cron jobs. Otherwise it prints nothing, which the ``no_agent`` cron
path treats as a silent run (no message).

Because the scan writes a complete, presentation-ready ``INVENTORY.md``, no LLM
is involved in delivery: what the scan produced is exactly what the user sees.
"""

import os
import sys
from pathlib import Path

SCAN_JOB_ID = "bootstrap-inventory-scan"
DELIVERY_JOB_ID = "bootstrap-inventory-delivery"


def _data_dir() -> Path:
    return Path(os.environ.get("HERMES_HOME", "/opt/data"))


def _should_deliver(data_dir: Path) -> bool:
    """True only when the report is ready, a human is present, and it has not
    been delivered yet."""
    if (data_dir / ".bootstrap_completed").exists():
        return False
    return (data_dir / "INVENTORY.md").exists() and (data_dir / ".user_aligned").exists()


def _cleanup(data_dir: Path) -> None:
    """Conclude onboarding after the report has been emitted to stdout.

    Marks ``.bootstrap_completed`` first: this is the idempotency guard that
    suppresses any further delivery and, together with the scan wake-gate,
    prevents the scan from re-running even after ``INVENTORY.md`` is removed.
    Then removes the single-use report and both onboarding cron jobs.

    Job removal uses the in-process ``cron.jobs`` API (file-locked), not a
    ``hermes cron rm`` subprocess. Every step is best-effort: onboarding has
    already succeeded by the time this runs, so a cleanup hiccup must never
    turn a delivered report into a reported failure. Even if job removal
    fails, ``.bootstrap_completed`` keeps both jobs inert.
    """
    completed = data_dir / ".bootstrap_completed"
    try:
        completed.touch(exist_ok=True)
    except OSError as e:
        sys.stderr.write(f"bootstrap_delivery: could not mark completed: {e}\n")

    try:
        (data_dir / "INVENTORY.md").unlink(missing_ok=True)
    except OSError as e:
        sys.stderr.write(f"bootstrap_delivery: could not remove INVENTORY.md: {e}\n")

    # Remove the onboarding jobs in-process. Self-removing the delivery job
    # mid-run is safe: the scheduler delivers this run's stdout from the job
    # dict it cached at tick time, and a subsequent mark_job_run on a missing
    # job simply logs a warning (see the plugin README, "Architectural Rules").
    try:
        from cron.jobs import remove_job  # type: ignore import-not-found
    except Exception:
        return
    for job_id in (SCAN_JOB_ID, DELIVERY_JOB_ID):
        try:
            remove_job(job_id)
        except Exception as e:
            sys.stderr.write(f"bootstrap_delivery: could not remove {job_id}: {e}\n")


def main(data_dir: Path | None = None) -> int:
    if data_dir is None:
        data_dir = _data_dir()

    if not _should_deliver(data_dir):
        return 0  # silent run — nothing to deliver yet (or already delivered)

    try:
        content = (data_dir / "INVENTORY.md").read_text(encoding="utf-8")
    except OSError as e:
        # Do not mark complete: leave state untouched so a later tick retries.
        sys.stderr.write(f"bootstrap_delivery: could not read INVENTORY.md: {e}\n")
        return 1

    sys.stdout.write(content)
    sys.stdout.flush()

    # Cleanup runs only after the report is safely on stdout (already captured
    # by the scheduler), so removing INVENTORY.md here cannot truncate delivery.
    _cleanup(data_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
