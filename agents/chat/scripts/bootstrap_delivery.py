#!/usr/bin/env python3
"""Deterministic (no-LLM) delivery for first-time onboarding.

This script backs the ``bootstrap-inventory-delivery`` cron job, which runs
with ``no_agent: true``. Its stdout is delivered verbatim by the cron
scheduler to the job's configured target (``deliver: origin`` — the chat the
user first spoke in, bound by the ``bootstrap_onboarding`` plugin).

Delivery happens exactly once, and only when discovery has finished AND a
human has connected:

- ``.user_aligned`` present -> a human has opened the chat (set by the plugin;
  never by a background task — see the plugin README).
- ``.bootstrap_completed`` absent -> the report has not been delivered yet.
- ``INVENTORY.md`` present  -> the background scan has produced the report.

The two markers are this pod's, and are checked first. The report is not: the
prioritization worker writes it through its terminal, and with the shell
sandbox on that terminal is the sandbox pod, whose data volume this pod does
not mount. So the report is read over ``sandbox_exec.read_bytes`` when the
sandbox is on, and from ``HERMES_HOME`` when it is off, and only once both
markers say a delivery is due — the ssh read is the one check with a cost.

When all three hold, the script claims delivery, prints ``INVENTORY.md``
(delivered verbatim) and sets the report aside where it was read. Otherwise it
prints nothing, which the ``no_agent`` cron path treats as a silent run (no
message). A report it cannot read, or one over ``REPORT_MAX_BYTES``, fails the
run instead (exit 1), which the scheduler posts as an alert. An unreachable
sandbox stays silent and is retried on the next tick until it has not answered
for ``SANDBOX_UNANSWERED_ALERT_SECONDS``, and fails the run after that. The
first run ``RETIRE_AFTER_SECONDS`` or more after a delivery removes the two
onboarding cron jobs; ``_retire_jobs`` says why the delivering run cannot.

The claim is what makes "exactly once" true rather than merely likely.
``.bootstrap_completed`` is created with ``O_CREAT | O_EXCL`` *before* anything
reaches stdout, so of two runs racing on the same report — a scheduled tick and
the plugin's ``trigger_job``, say — exactly one can win the create and emit;
the loser exits silently. Checking the marker and then writing it after
delivery would leave both runs inside the same window, and the user would be
sent the entire onboarding report twice.

Because the prioritization stage writes a finished, presentation-ready
``INVENTORY.md``, no LLM is involved in delivery: what that stage produced is
exactly what the user sees. The sweep's complete findings are a different file
(``INVENTORY.raw.md``) and are never delivered from here.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import sandbox_exec

SCAN_JOB_ID = "bootstrap-inventory-scan"
DELIVERY_JOB_ID = "bootstrap-inventory-delivery"

# The delivered report is renamed here rather than deleted. It is the only copy
# of a sweep that can take many minutes over a whole fleet, and a chat message
# is easy to lose; keeping it means a re-send is a `cat`, not a re-scan.
DELIVERED_REPORT_NAME = "INVENTORY.delivered.md"
REPORT_NAME = "INVENTORY.md"

# The sandbox's data volume, whatever HERMES_HOME says on this side: the same
# path by construction (deploy/sandbox/Dockerfile), and a separate constant
# because it names a directory on the far side of the connection.
SANDBOX_HOME = "/opt/data"

# Far above the report the prioritization stage writes, which is sized for one
# chat message. It bounds what one tick moves over ssh; a report past it is
# refused rather than cut, since a truncated report would be delivered as whole.
REPORT_MAX_BYTES = 256 * 1024
SANDBOX_TIMEOUT_SECONDS = 30

# How old ``.bootstrap_completed`` must be before a run removes the jobs. A
# younger marker may belong to a racing run that is still delivering (see the
# claim in the module docstring), and removing the delivery job under it would discard its
# report. Far above that run's post-claim work: a stdout write and one archive
# over ssh bounded by SANDBOX_TIMEOUT_SECONDS.
RETIRE_AFTER_SECONDS = 300

# Created by the first read the sandbox does not answer and removed by the next
# one it does, so its mtime is when the sandbox stopped answering. Past
# SANDBOX_UNANSWERED_ALERT_SECONDS the sandbox is not rolling: SandboxUnavailable
# also covers a rejected key, a changed host key and a config with no ssh_host,
# none of which clear on their own. Far above a sandbox pod restart.
UNANSWERED_MARKER = ".bootstrap_sandbox_unanswered"
SANDBOX_UNANSWERED_ALERT_SECONDS = 900

# By absolute path, so no PATH entry picks the binary. A function defined under
# this name in the ~/.bashrc the model owns still shadows it, since bash allows a
# slash in a function name; that loses only the rename, as the claim comes first.
REMOTE_MV = "/bin/mv"


def _data_dir() -> Path:
    return Path(os.environ.get("HERMES_HOME", "/opt/data"))


def _awaiting_delivery(data_dir: Path) -> bool:
    """True when a human is present and the report has not been delivered yet.

    Both markers live on this pod, so this costs two stats; it runs before the
    report read, which may cross into the sandbox.
    """
    if (data_dir / ".bootstrap_completed").exists():
        return False
    return (data_dir / ".user_aligned").exists()


def _completed_at(data_dir: Path) -> float | None:
    """When the delivery claim was taken, or None if it has not been."""
    try:
        return (data_dir / ".bootstrap_completed").stat().st_mtime
    except FileNotFoundError:
        return None


def _read_report(data_dir: Path, in_sandbox: bool) -> bytes | None:
    """Up to ``REPORT_MAX_BYTES + 1`` bytes of the report, or None if there is none.

    Raises ``sandbox_exec.SandboxUnavailable`` or ``subprocess.TimeoutExpired``
    when the sandbox did not answer, ``sandbox_exec.SandboxReadFailed`` when the
    read ran and did not return the report, and ``OSError`` when the local file
    could not be read.
    """
    if in_sandbox:
        return sandbox_exec.read_bytes(
            f"{SANDBOX_HOME}/{REPORT_NAME}",
            max_bytes=REPORT_MAX_BYTES + 1,
            timeout=SANDBOX_TIMEOUT_SECONDS,
        )
    try:
        with open(data_dir / REPORT_NAME, "rb") as handle:
            return handle.read(REPORT_MAX_BYTES + 1)
    except FileNotFoundError:
        return None


def _claim_delivery(data_dir: Path) -> bool:
    """Atomically claim the right to deliver the report. True if we won.

    ``O_CREAT | O_EXCL`` is a single filesystem operation, so this is the point
    at which "may I deliver?" and "I am delivering" become indivisible. Called
    before the first byte of the report is written to stdout.

    A False return means another run already claimed it: the caller must emit
    nothing at all.
    """
    completed = data_dir / ".bootstrap_completed"
    try:
        fd = os.open(str(completed), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False  # another run got there first
    except OSError as e:
        # Cannot claim -> cannot safely deliver. Staying silent costs a retry
        # next tick; delivering unclaimed risks sending the report twice.
        sys.stderr.write(f"bootstrap_delivery: could not claim delivery: {e}\n")
        return False
    os.close(fd)
    return True


def _archive_report(data_dir: Path, in_sandbox: bool) -> None:
    """Rename the delivered report to ``DELIVERED_REPORT_NAME`` where it was read."""
    if not in_sandbox:
        try:
            report = data_dir / REPORT_NAME
            if report.exists():
                report.replace(data_dir / DELIVERED_REPORT_NAME)
        except OSError as e:
            sys.stderr.write(f"bootstrap_delivery: could not archive INVENTORY.md: {e}\n")
        return
    # As the terminal's login: the sandbox's /opt/data is that account's and
    # mode 755, so the default login cannot rename inside it.
    try:
        moved = sandbox_exec.run(
            [REMOTE_MV, "-f", "--", f"{SANDBOX_HOME}/{REPORT_NAME}", f"{SANDBOX_HOME}/{DELIVERED_REPORT_NAME}"],
            principal=sandbox_exec.TERMINAL_PRINCIPAL,
            timeout=SANDBOX_TIMEOUT_SECONDS,
        )
    except Exception as e:
        sys.stderr.write(f"bootstrap_delivery: could not archive INVENTORY.md in the sandbox: {e}\n")
        return
    if moved.returncode != 0:
        sys.stderr.write(
            "bootstrap_delivery: could not archive INVENTORY.md in the sandbox: "
            f"{(moved.stderr or '').strip()}\n"
        )


def _cleanup(data_dir: Path, in_sandbox: bool) -> None:
    """Tidy up after the report has been emitted to stdout.

    Onboarding is already marked complete by the delivery claim, so everything
    here is best-effort: a cleanup hiccup must never turn a delivered report
    into a reported failure.

    The report is renamed, not deleted — see ``DELIVERED_REPORT_NAME``. Moving
    it out of the way still matters: the sweep and prioritization SOPs, which
    run where the report is, treat a present ``INVENTORY.md`` as "already done",
    so leaving it in place would make a later, deliberate re-run of onboarding a
    no-op.
    """
    _archive_report(data_dir, in_sandbox)


def _retire_jobs() -> None:
    """Remove both onboarding cron jobs, in-process.

    Only a run with nothing to deliver may call this. Removing a job while it
    runs drops the run's fire claim, and the scheduler then discards that run's
    output instead of posting it. So the run that delivers the report leaves
    both jobs in place, and a later run, which finds ``.bootstrap_completed``
    and has nothing to post, removes them and loses nothing. The delivery job
    goes last because removing it ends this run.
    """
    try:
        from cron.jobs import remove_job  # type: ignore import-not-found
    except Exception:
        return
    for job_id in (SCAN_JOB_ID, DELIVERY_JOB_ID):
        try:
            remove_job(job_id)
        except Exception as e:
            sys.stderr.write(f"bootstrap_delivery: could not remove {job_id}: {e}\n")


def _sandbox_unanswered(data_dir: Path, error: Exception) -> int:
    """Stay silent while the sandbox may be rolling, and fail once it has been gone too long.

    Silent at first because a non-zero exit is posted to the user's chat as a
    failure alert on every tick, and a sandbox restart is not the user's
    problem. See ``UNANSWERED_MARKER`` for why the silence ends.
    """
    marker = data_dir / UNANSWERED_MARKER
    try:
        marker.touch(exist_ok=False)
    except FileExistsError:
        pass
    except OSError as e:
        sys.stderr.write(f"bootstrap_delivery: could not record the unanswered read: {e}\n")
    try:
        unanswered_for = time.time() - marker.stat().st_mtime
    except OSError:
        unanswered_for = 0.0
    if unanswered_for < SANDBOX_UNANSWERED_ALERT_SECONDS:
        sys.stderr.write(f"bootstrap_delivery: the shell sandbox did not answer: {error}\n")
        return 0
    sys.stderr.write(
        f"bootstrap_delivery: the shell sandbox has not answered for {int(unanswered_for)} s: {error}\n"
    )
    return 1


def _clear_unanswered(data_dir: Path) -> None:
    try:
        (data_dir / UNANSWERED_MARKER).unlink(missing_ok=True)
    except OSError as e:
        sys.stderr.write(f"bootstrap_delivery: could not clear {UNANSWERED_MARKER}: {e}\n")


def main(data_dir: Path | None = None) -> int:
    if data_dir is None:
        data_dir = _data_dir()

    completed = _completed_at(data_dir)
    if completed is not None:
        if time.time() - completed >= RETIRE_AFTER_SECONDS:
            _retire_jobs()
        return 0

    if not _awaiting_delivery(data_dir):
        return 0  # silent run — nobody to deliver to yet

    try:
        in_sandbox = sandbox_exec.sandbox_enabled()
    except sandbox_exec.SandboxMisconfigured as e:
        sys.stderr.write(f"bootstrap_delivery: cannot tell where INVENTORY.md is: {e}\n")
        return 1

    # Read before claiming, so a read failure leaves no claim behind to undo
    # and the next tick retries cleanly.
    try:
        raw = _read_report(data_dir, in_sandbox)
    except (sandbox_exec.SandboxUnavailable, subprocess.TimeoutExpired) as e:
        return _sandbox_unanswered(data_dir, e)
    except (OSError, sandbox_exec.SandboxMisconfigured, sandbox_exec.SandboxReadFailed) as e:
        _clear_unanswered(data_dir)
        sys.stderr.write(f"bootstrap_delivery: could not read INVENTORY.md: {e}\n")
        return 1
    _clear_unanswered(data_dir)
    if raw is None:
        return 0  # silent run — the report is not written yet
    if len(raw) > REPORT_MAX_BYTES:
        sys.stderr.write(
            f"bootstrap_delivery: INVENTORY.md is larger than {REPORT_MAX_BYTES} bytes; not delivering it\n"
        )
        return 1
    content = raw.decode("utf-8", errors="replace")

    # The cheap check above is advisory; this is the decision. Nothing may be
    # written to stdout before it succeeds.
    if not _claim_delivery(data_dir):
        return 0  # another run is delivering this report — stay silent

    sys.stdout.write(content)
    sys.stdout.flush()

    # Cleanup runs only after the report is safely on stdout (already captured
    # by the scheduler), so removing INVENTORY.md here cannot truncate delivery.
    _cleanup(data_dir, in_sandbox)
    return 0


if __name__ == "__main__":
    sys.exit(main())
