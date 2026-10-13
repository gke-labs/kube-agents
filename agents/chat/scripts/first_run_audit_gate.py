#!/usr/bin/env python3
"""Dispatcher for the ``first-run-quick-value-audit`` cron job.

After the bootstrap inventory scan completes, this job triggers four
high-value governance audits on the Platform Agent's cron roster so a new
install surfaces actionable findings within the first two hours:

1. ``fleet-wide-cost-analysis`` (Fleet Waste Audit)
2. ``compliance-audit`` (Security & RBAC Posture Audit)
3. ``obtainability-audit`` (Workload Reliability Audit)
4. ``stockout-prevention`` (Stockout Prevention & Capacity Audit)

On standard installs where the ``oobe`` cron job (``oobe.py``) is active on the
Planning Agent's roster or has already recorded ``.oobe_audits_fired``, this
gate yields to ``oobe`` so the two jobs never double-trigger the audits, and
retires itself once ``oobe`` marks its chain done.

When ``oobe`` is not driving the chain (or when an operator explicitly refreshes
``/opt/data/INVENTORY.raw.md`` after onboarding), this script detects bootstrap
readiness via either a fresh ``INVENTORY.raw.md`` on ``HERMES_HOME`` or a
settled onboarding scan in ``.bootstrap_scan_filed`` / ``kanban.db`` (covering
``shellSandbox.enabled: true`` installs where ``INVENTORY.raw.md`` lives on the
sandbox volume). It honours disabled/paused holds on the Platform Agent's
roster, waits if an audit is already in flight, adopts audits that have already
completed since bootstrap, atomically pre-records each audit ID in
``.first-run-audits-filed`` before invoking
``HERMES_HOME=<data_dir>/profiles/platform hermes cron run <job-id>``, and
removes itself from the cron store once complete.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import oobe  # beside this script in the pod

FIRST_RUN_JOB_ID = "first-run-quick-value-audit"

# Marker file names (relative to data_dir)
BOOTSTRAP_COMPLETE_MARKER_NAME = "INVENTORY.raw.md"
FIRST_RUN_FILED_MARKER_NAME = ".first-run-audits-filed"

# Only fire when bootstrap completed recently (within 2 hours).
# Existing clusters keep onboarding markers on disk permanently after onboarding;
# bounding the age prevents an image upgrade from triggering all four audits
# across already-onboarded fleets.
MAX_BOOTSTRAP_AGE_SECONDS = 2 * 3600

PLATFORM_PROFILE_REL = Path("profiles") / "platform"

# Canonical audit job IDs on agents/platform/cron/jobs.json
FIRST_RUN_AUDITS = oobe.FIRST_RUN_AUDITS


def _data_dir() -> Path:
    """Return the data directory, respecting HERMES_HOME."""
    return Path(os.environ.get("HERMES_HOME", "/opt/data"))


def _bootstrap_complete_marker(data_dir: Path) -> Path:
    return data_dir / BOOTSTRAP_COMPLETE_MARKER_NAME


def _first_run_filed_marker(data_dir: Path) -> Path:
    return data_dir / FIRST_RUN_FILED_MARKER_NAME


def _write_marker(data_dir: Path, payload: dict) -> None:
    """Atomically write ``.first-run-audits-filed`` via a temporary sibling."""
    marker = _first_run_filed_marker(data_dir)
    tmp = marker.with_name(marker.name + oobe.TMP_SUFFIX)
    try:
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(marker)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _already_triggered_jobs(data_dir: Path) -> list[str]:
    """Return audit IDs already recorded in ``.first-run-audits-filed``."""
    marker = _first_run_filed_marker(data_dir)
    if not marker.exists():
        return []
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(data, dict) and isinstance(data.get("jobs"), list):
        return [j for j in data["jobs"] if isinstance(j, str)]
    return list(FIRST_RUN_AUDITS)


def _pending_audits(data_dir: Path) -> list[str]:
    """Return audit IDs from ``FIRST_RUN_AUDITS`` not yet recorded in the marker."""
    done = set(_already_triggered_jobs(data_dir))
    return [job_id for job_id in FIRST_RUN_AUDITS if job_id not in done]


def _manual_inventory_refresh_after(data_dir: Path, reference_mtime: float) -> bool:
    """Return True if INVENTORY.raw.md exists and was touched after reference_mtime."""
    bootstrap = _bootstrap_complete_marker(data_dir)
    if not bootstrap.exists():
        return False
    try:
        return bootstrap.stat().st_mtime > reference_mtime
    except OSError:
        return False


def _oobe_enabled_on_chat_roster(data_dir: Path) -> bool:
    """Return True if the chat cron roster has an enabled ``oobe`` entry."""
    roster = data_dir / "cron" / "jobs.json"
    if not roster.is_file():
        return False
    try:
        stored = json.loads(roster.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    jobs = stored.get("jobs", []) if isinstance(stored, dict) else stored
    if not isinstance(jobs, list):
        return False
    return any(
        isinstance(job, dict)
        and job.get("id") == oobe.OOBE_JOB_ID
        and job.get("enabled", True)
        for job in jobs
    )


def _oobe_active_or_done(data_dir: Path) -> tuple[bool, bool]:
    """Return ``(yield_to_oobe, oobe_done)`` so this gate never collides with ``oobe``."""
    oobe_marker = data_dir / oobe.AUDITS_MARKER
    if oobe_marker.is_file():
        state = oobe.read_state(data_dir)
        if state:
            try:
                marker_mtime = oobe_marker.stat().st_mtime
            except OSError:
                marker_mtime = float("inf")
            done = bool(state.get(oobe.STATE_DONE))
            if not (done and _manual_inventory_refresh_after(data_dir, marker_mtime)):
                return True, done

    scan_marker = data_dir / oobe.SCAN_FILED_MARKER
    if scan_marker.is_file() and _oobe_enabled_on_chat_roster(data_dir):
        return True, False

    return False, False


def _bootstrap_ready_at(data_dir: Path, current: float) -> tuple[Optional[float], bool]:
    """Return ``(ready_timestamp, is_stale)`` from INVENTORY.raw.md or settled scan state."""
    stale = False
    bootstrap = _bootstrap_complete_marker(data_dir)
    if bootstrap.exists():
        try:
            mtime = bootstrap.stat().st_mtime
        except OSError:
            return None, False
        if current - mtime <= MAX_BOOTSTRAP_AGE_SECONDS:
            return mtime, False
        stale = True

    filed = oobe.scan_filed(data_dir)
    if filed is not None:
        _sweep_id, filed_at = filed
        if current - filed_at > MAX_BOOTSTRAP_AGE_SECONDS:
            return None, True
        if oobe.scan_settled(data_dir, current):
            return filed_at, False
        return None, False

    return None, stale


def retire() -> None:
    """Remove this job in-process once first-run audits are complete or superseded."""
    try:
        from cron.jobs import remove_job  # type: ignore import-not-found
    except Exception:  # noqa: BLE001 - outside the gateway
        return
    try:
        remove_job(FIRST_RUN_JOB_ID)
    except Exception as e:  # noqa: BLE001 - the next run tries again
        print(f"Could not remove the {FIRST_RUN_JOB_ID} job: {e}", file=sys.stderr)


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
    if not _pending_audits(data_dir):
        return True
    yield_to_oobe, _ = _oobe_active_or_done(data_dir)
    if yield_to_oobe:
        return True
    current = time.time() if now is None else now
    ready_at, _stale = _bootstrap_ready_at(data_dir, current)
    return ready_at is None


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


def main(data_dir: Optional[Path] = None, *, now: Optional[float] = None) -> int:
    """Main entry point. Accepts data_dir and now for testing."""
    if data_dir is None:
        data_dir = _data_dir()
    current = time.time() if now is None else now

    if not _pending_audits(data_dir):
        retire()
        return 0

    yield_to_oobe, oobe_done = _oobe_active_or_done(data_dir)
    if yield_to_oobe:
        if oobe_done:
            retire()
        return 0

    ready_at, stale = _bootstrap_ready_at(data_dir, current)
    if ready_at is None:
        if stale:
            retire()
        return 0

    runs = oobe._runs(data_dir, FIRST_RUN_AUDITS)
    if runs is None:
        return 0
    if oobe.audits_in_flight(runs, current):
        return 0

    roster_path = data_dir / PLATFORM_PROFILE_REL / oobe.CRON_DIR / oobe.ROSTER_FILE
    if roster_path.exists():
        holds = oobe.audit_holds(data_dir)
        if holds is None:
            return 0
    else:
        holds = {}

    recorded_jobs = _already_triggered_jobs(data_dir)
    pending = _pending_audits(data_dir)

    print(
        "Bootstrap complete, triggering first-run quick value audits...",
        file=sys.stderr,
    )

    triggered_jobs = []
    failed_audits = []

    for job_id in pending:
        if oobe.completed_since(runs, job_id, ready_at):
            recorded_jobs.append(job_id)
            _write_marker(data_dir, {"filed_at": current, "jobs": recorded_jobs})
            continue
        if job_id in holds:
            recorded_jobs.append(job_id)
            _write_marker(data_dir, {"filed_at": current, "jobs": recorded_jobs})
            continue

        # Persist job_id atomically before triggering so an interrupted run
        # cannot re-trigger an audit that was already marked due.
        recorded_jobs.append(job_id)
        _write_marker(data_dir, {"filed_at": current, "jobs": recorded_jobs})

        if trigger_audit(job_id, data_dir):
            triggered_jobs.append(job_id)
            print(f"Triggered {job_id} on platform cron roster", file=sys.stderr)
        else:
            recorded_jobs.remove(job_id)
            if recorded_jobs:
                _write_marker(data_dir, {"filed_at": current, "jobs": recorded_jobs})
            else:
                _first_run_filed_marker(data_dir).unlink(missing_ok=True)
            failed_audits.append(job_id)

    if failed_audits:
        print(
            f"Failed to trigger {len(failed_audits)} audits: {failed_audits}. "
            "Will retry on next tick.",
            file=sys.stderr,
        )
        return 1

    print(
        f"First-run audits triggered: {len(recorded_jobs)} jobs",
        file=sys.stderr,
    )
    if not _pending_audits(data_dir):
        retire()
    return 0


if __name__ == "__main__":
    sys.exit(main())
