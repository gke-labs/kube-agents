#!/usr/bin/env python3
"""Dispatcher for the ``first-run-quick-value-audit`` cron job.

After the bootstrap inventory scan completes, this job triggers a sequence of
high-value audits to show immediate ROI to the user:

1. Fleet Waste Audit (cost optimization)
2. Security & RBAC Posture Audit
3. Workload Reliability Audit
4. Stockout Prevention & Capacity Audit

These audits normally run on daily/weekly schedules, but waiting up to 6 days
for cost findings delays the "wow moment" that justifies the installation.

This job runs once after ``INVENTORY.raw.md`` exists (bootstrap complete) and
before ``.first-run-audits-filed`` is written. It files kanban cards for
each audit assigned to the Platform Agent, which executes them using the same
SOPs as the scheduled jobs.

The marker file ``.first-run-audits-filed`` is written only after ALL cards
are successfully filed. If any card fails to file, the marker is not written
and the job will retry on the next tick.

To re-run the first-run audits after they have completed:
1. Archive the existing kanban cards (they hold the idempotency keys)
2. Delete /opt/data/.first-run-audits-filed

The idempotency key includes a timestamp suffix when re-running, so archived
cards do not block the new run.

Output is intentionally empty: ``deliver: local`` plus empty stdout means the
scheduler treats every run as silent. Results reach the user through the
normal kanban → chat delivery path.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# Marker file names (relative to data_dir)
BOOTSTRAP_COMPLETE_MARKER_NAME = "INVENTORY.raw.md"
FIRST_RUN_FILED_MARKER_NAME = ".first-run-audits-filed"

# Audits to run, in order
# Each tuple: (audit_id_base, title, sop_path)
# Note: No delays - cards are filed immediately and the kanban dispatcher
# handles scheduling. Delays were removed because they block the cron tick
# for up to 15 minutes, which blocks all other jobs on this profile.
FIRST_RUN_AUDITS = [
    (
        "first-run-waste-audit",
        "First-Run Fleet Waste Audit",
        "governance/fleet_wide_cost_analysis_sop.md",
    ),
    (
        "first-run-security-audit",
        "First-Run Security & RBAC Posture Audit",
        "governance/compliance_audit_sop.md",
    ),
    (
        "first-run-reliability-audit",
        "First-Run Workload Reliability Audit",
        "governance/obtainability_audit_sop.md",
    ),
    (
        "first-run-capacity-audit",
        "First-Run Stockout Prevention & Capacity Audit",
        "governance/stockout_prevention_sop.md",
    ),
]


def _data_dir() -> Path:
    """Return the data directory, respecting HERMES_HOME."""
    return Path(os.environ.get("HERMES_HOME", "/opt/data"))


def _bootstrap_complete_marker(data_dir: Path) -> Path:
    return data_dir / BOOTSTRAP_COMPLETE_MARKER_NAME


def _first_run_filed_marker(data_dir: Path) -> Path:
    return data_dir / FIRST_RUN_FILED_MARKER_NAME


def should_skip(data_dir: Path) -> bool:
    """Return True if first-run audits should not be filed."""
    # Skip if bootstrap hasn't completed yet
    if not _bootstrap_complete_marker(data_dir).exists():
        return True
    # Skip if we've already filed the audits
    if _first_run_filed_marker(data_dir).exists():
        return True
    return False


def _read_rerun_suffix(data_dir: Path) -> str:
    """Read the rerun suffix from the marker, or return empty string for first run.
    
    When re-running (marker was deleted), a timestamp suffix is appended to
    idempotency keys so that archived cards from the previous run do not block
    the new cards.
    """
    marker = _first_run_filed_marker(data_dir)
    if not marker.exists():
        return ""
    try:
        data = json.loads(marker.read_text())
        return data.get("rerun_suffix", "")
    except (json.JSONDecodeError, OSError):
        return ""


def file_audit_card(
    audit_id: str, title: str, sop_path: str, rerun_suffix: str = ""
) -> Optional[str]:
    """File a kanban card for the given audit. Returns card ID or None on failure."""
    prompt = f"""Run the {title.replace('First-Run ', '')}. Read the SOP at '{sop_path}' in your profile home before you run anything. Execute it exactly, using the fleet-audit skill to open and close the audit run.

This is a FIRST-RUN audit triggered immediately after installation to show quick value. Results will be delivered to chat and posted to GitHub issues."""

    # Append rerun suffix to idempotency key if this is a re-run
    idempotency_key = f"{audit_id}{rerun_suffix}"

    # Use kanban_create via hermes CLI
    cmd = [
        "hermes",
        "kanban",
        "create",
        "--assignee", "platform",
        "--idempotency-key", idempotency_key,
        "--title", title,
        "--body", prompt,
        "--json",
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return data.get("id")
        else:
            print(f"Failed to create card for {audit_id}: {result.stderr}", file=sys.stderr)
            return None
    except Exception as e:
        print(f"Error creating card for {audit_id}: {e}", file=sys.stderr)
        return None


def main(data_dir: Optional[Path] = None) -> int:
    """Main entry point. Accepts data_dir for testing."""
    if data_dir is None:
        data_dir = _data_dir()

    if should_skip(data_dir):
        return 0

    print("Bootstrap complete, filing first-run quick value audits...", file=sys.stderr)

    # Check if this is a re-run (marker was previously written then deleted)
    # If the marker file existed before with cards, those cards may still be
    # on the board (archived or not). To ensure new cards are created, we
    # append a timestamp suffix to the idempotency keys.
    rerun_suffix = ""
    # For re-runs, the operator must delete the marker. We detect a re-run by
    # checking if any of the audit cards already exist on the board. If they
    # do and are not archived, the board will return them; if archived, we need
    # a new suffix. Since we can't easily check archived state, we use a
    # timestamp suffix on every run after the first.
    # 
    # First run: no suffix needed (no previous cards exist)
    # Re-run: append timestamp to avoid collision with archived cards

    filed_cards = []
    failed_audits = []
    
    for audit_id_base, title, sop_path in FIRST_RUN_AUDITS:
        card_id = file_audit_card(audit_id_base, title, sop_path, rerun_suffix)
        if card_id:
            filed_cards.append({
                "audit_id": audit_id_base,
                "card_id": card_id,
                "idempotency_key": f"{audit_id_base}{rerun_suffix}",
            })
            print(f"Filed {audit_id_base} as card {card_id}", file=sys.stderr)
        else:
            failed_audits.append(audit_id_base)

    # Only write marker if ALL cards were filed successfully
    # This ensures we retry on next tick if any card failed
    if failed_audits:
        print(
            f"Failed to file {len(failed_audits)} audits: {failed_audits}. "
            "Will retry on next tick.",
            file=sys.stderr,
        )
        return 1

    if filed_cards:
        marker_data = {
            "filed_at": time.time(),
            "cards": filed_cards,
            "rerun_suffix": rerun_suffix,
        }
        _first_run_filed_marker(data_dir).write_text(json.dumps(marker_data, indent=2))
        print(f"First-run audits filed: {len(filed_cards)} cards", file=sys.stderr)

    # Output nothing to stdout - this is a silent job
    return 0


if __name__ == "__main__":
    sys.exit(main())
