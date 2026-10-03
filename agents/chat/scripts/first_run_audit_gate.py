#!/usr/bin/env python3
"""Dispatcher for the ``first-run-quick-value-audit`` cron job.

After the bootstrap inventory scan completes, this job triggers four
high-value governance audits on the Platform Agent's cron roster so a new
install surfaces actionable findings within the first two hours:

1. ``fleet-wide-cost-analysis`` (Fleet Waste Audit)
2. ``compliance-audit`` (Security & RBAC Posture Audit)
3. ``obtainability-audit`` (Workload Reliability Audit)
4. ``stockout-prevention`` (Stockout Prevention & Capacity Audit)

Rather than filing ad-hoc kanban cards, this script invokes
``HERMES_HOME=<data_dir>/profiles/platform hermes cron run <job-id>`` for each
audit. That marks each existing entry due on the Platform Agent's roster so the
next ``profile-cron-tick`` dispatches it through the standard cron path — with
its shipped prompt and SOP line-number guards verbatim, ``fleet-audit`` skill
preloaded, per-job ``cron/.job-<id>.lock`` held, and ``deliver: "chat"``
active.

The job runs once when ``INVENTORY.raw.md`` is present, was written within the
last ``MAX_BOOTSTRAP_AGE_SECONDS`` (2 hours, preventing existing installs from
firing all four audits on an image upgrade), and ``.first-run-audits-filed``
does not yet exist. The marker is written only after all four ``hermes cron
run`` calls succeed; if any call fails, the marker is omitted so the next tick
retries.

To manually re-run the sequence through this gate, refresh
``/opt/data/INVENTORY.raw.md`` (``touch /opt/data/INVENTORY.raw.md``) and
delete ``/opt/data/.first-run-audits-filed``, or trigger individual streams
directly with ``HERMES_HOME=/opt/data/profiles/platform hermes cron run <id>``.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# Marker file names (relative to data_dir)
BOOTSTRAP_COMPLETE_MARKER_NAME = "INVENTORY.raw.md"
FIRST_RUN_FILED_MARKER_NAME = ".first-run-audits-filed"

# Only fire when INVENTORY.raw.md was written recently (within 2 hours).
# Existing clusters keep INVENTORY.raw.md on disk permanently after onboarding;
# bounding the age prevents an image upgrade from triggering all four audits
# across already-onboarded fleets.
MAX_BOOTSTRAP_AGE_SECONDS = 2 * 3600

PLATFORM_PROFILE_REL = Path("profiles") / "platform"

# Canonical audit job IDs on agents/platform/cron/jobs.json
FIRST_RUN_AUDITS = (
    "fleet-wide-cost-analysis",
    "compliance-audit",
    "obtainability-audit",
    "stockout-prevention",
)


def _data_dir() -> Path:
    """Return the data directory, respecting HERMES_HOME."""
    return Path(os.environ.get("HERMES_HOME", "/opt/data"))


def _bootstrap_complete_marker(data_dir: Path) -> Path:
    return data_dir / BOOTSTRAP_COMPLETE_MARKER_NAME


def _first_run_filed_marker(data_dir: Path) -> Path:
    return data_dir / FIRST_RUN_FILED_MARKER_NAME


def hermes_bin() -> Path:
    """Locate ``hermes``, preferring the binary beside the running interpreter."""
    sibling = Path(sys.executable).with_name("hermes")
    if sibling.is_file():
        return sibling
    found = shutil.which("hermes")
    if found:
        return Path(found)
    raise FileNotFoundError(f"no `hermes` beside {sys.executable} or on PATH")


def should_skip(data_dir: Path, *, now: Optional[float] = None) -> bool:
    """Return True if first-run audits should not be triggered on this tick."""
    if _first_run_filed_marker(data_dir).exists():
        return True
    bootstrap = _bootstrap_complete_marker(data_dir)
    if not bootstrap.exists():
        return True
    try:
        mtime = bootstrap.stat().st_mtime
    except OSError:
        return True
    current = time.time() if now is None else now
    if current - mtime > MAX_BOOTSTRAP_AGE_SECONDS:
        return True
    return False


def trigger_audit(job_id: str, data_dir: Path) -> bool:
    """Mark a Platform Agent cron job due via ``hermes cron run <job_id>``."""
    try:
        binary = str(hermes_bin())
    except FileNotFoundError as e:
        print(f"Error locating hermes for {job_id}: {e}", file=sys.stderr)
        return False

    env = os.environ.copy()
    env["HERMES_HOME"] = str(data_dir / PLATFORM_PROFILE_REL)
    cmd = [binary, "cron", "run", job_id]

    try:
        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return True
        print(
            f"Failed to trigger {job_id} (exit {result.returncode}): {result.stderr}",
            file=sys.stderr,
        )
        return False
    except Exception as e:
        print(f"Error triggering {job_id}: {e}", file=sys.stderr)
        return False


def main(data_dir: Optional[Path] = None) -> int:
    """Main entry point. Accepts data_dir for testing."""
    if data_dir is None:
        data_dir = _data_dir()

    if should_skip(data_dir):
        return 0

    print(
        "Bootstrap complete, triggering first-run quick value audits...",
        file=sys.stderr,
    )

    triggered_jobs = []
    failed_audits = []

    for job_id in FIRST_RUN_AUDITS:
        if trigger_audit(job_id, data_dir):
            triggered_jobs.append(job_id)
            print(f"Triggered {job_id} on platform cron roster", file=sys.stderr)
        else:
            failed_audits.append(job_id)

    if failed_audits:
        print(
            f"Failed to trigger {len(failed_audits)} audits: {failed_audits}. "
            "Will retry on next tick.",
            file=sys.stderr,
        )
        return 1

    marker_data = {
        "filed_at": time.time(),
        "jobs": triggered_jobs,
    }
    _first_run_filed_marker(data_dir).write_text(json.dumps(marker_data, indent=2))
    print(f"First-run audits triggered: {len(triggered_jobs)} jobs", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
