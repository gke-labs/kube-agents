#!/usr/bin/env python3
"""One feedback request per install, a week after the job first runs.

Backs the ``feedback-prompt`` cron job, which runs with ``no_agent: true`` and
``deliver: "chat"``: whatever this prints is handed to the Chat Agent to post in
the install's home channel, and printing nothing relays nothing. Every feedback
channel kube-agents has is reporter-initiated; this is the one place the product
asks.

The job is a daily cron entry that fires once, not a Hermes one-shot. A shipped
roster entry cannot carry an absolute ``run_at``, and a completed one-shot is
pruned from the store after seven days, at which point the start-up merge sees
an id the volume lacks and re-adds it, re-arming the prompt. So the once-only
state lives outside the store, in two marker files in the profile home:

- ``.feedback_prompt_armed`` holds the anchor: the time of the first tick with
  the prompt enabled. Created with ``O_CREAT | O_EXCL`` and never rewritten.
- ``.feedback_prompt_sent`` is the claim, and the record of the attempts. It is
  created with ``O_CREAT | O_EXCL`` *before* anything reaches stdout, so of two
  runs racing on the same home (a scheduled tick and an on-demand run, say)
  exactly one wins the create and prints; the loser exits silently. Checking
  the marker and writing it after printing would leave both runs inside the
  same window, and the channel would get the request twice. Its first line is
  the time of the latest attempt, its second the number of attempts so far.

Printing is not delivering. The scheduler relays the output after the run and
writes the outcome into this job's entry in the profile's ``cron/jobs.json``,
as ``last_delivery_error`` (the relay answered 502, was unreachable, no
platform bound, or ``None`` when the message landed), which is the record
``chat_delivery_watch.py`` grades. A claim taken and never delivered would
otherwise spend the one message on a relay outage, or on an install whose
operator binds a chat platform after the day the prompt fell due. So each later
tick reads the record of the run that carried the latest attempt: when it
grades as a hard failure (nothing reached any platform), the message is printed
again and the marker rewritten with the new attempt, under a lock on the marker
so two racing runs cannot both retry; a record that says delivered, partial or
degraded (it landed somewhere), a run the scheduler never recorded, or an
unreadable store ends the retries, in the direction of not posting twice. The
retries stop after ``MAX_ATTEMPTS`` in all, because every attempt is a Chat
Agent composition and an install that has bound no chat platform in two weeks
of daily tries is not one this message can reach. The text is a constant, so a
lost message is nothing to recover; only the posting is retried.

Both markers live on the data volume, which survives pod restarts and image
rolls and is deleted only by an uninstall. That is what makes an upgrade not
count as a new install: the markers outlive it, so the prompt neither re-arms
nor re-fires.

Two knobs, read from the environment, which is how the roster's other scripts
take their per-install settings (the operator copies them from the CR's
``spec.deployment.env`` through its allowlist):

- ``FEEDBACK_PROMPT_ENABLED`` (default ``true``). ``false`` means the run does
  nothing at all: it neither arms nor claims, so an install that turns the
  prompt on later still gets exactly one request, a delay after it did so.
- ``FEEDBACK_PROMPT_DELAY`` (default ``7d``; ``<n>d``, ``<n>h`` or ``<n>m``). A
  value that does not parse is a failed run, exit 1 with the reason on stderr,
  which the scheduler reports in chat like any other script failure until the
  value is fixed. Falling back silently was the alternative, and it is not
  observable: the scheduler keeps a zero-exit script's stderr nowhere, so the
  only trace of a rejected value would be the message arriving a week early.

The schedule is daily, so the delay is quantised to the 13:00 UTC tick: the
message lands on the first tick at or after the anchor plus the delay. A delay
of a day or more is compared with a few minutes of slack, because the tick's
own time drifts by seconds from day to day (the ticker's phase within its
minute changes on every pod restart, and start-up takes a variable few
seconds) and without the slack a week could read as seven days less a few
seconds and land on day eight. A delay under a day is compared exactly: the
daily schedule fires it at the next tick regardless, and exactness is what a
two-minute delay is for when a run is marked due by hand.

The form URL is a constant, not a knob: the published short link is the only
address the maintainers hand out, and it redirects wherever the form lives.
"""

import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path

# A sibling in `$HERMES_HOME/scripts`, this script's own directory and therefore
# `sys.path[0]` when the scheduler runs it. It owns the grading of a
# `last_delivery_error`, from the strings the chat adapter produces.
from chat_delivery_watch import GRADE_HARD, grade_error, is_delivered_note, parse_iso

HOME_ENV = "HERMES_HOME"
# What the ticker sets HERMES_HOME to for this roster is the profile home;
# the default is the gateway's, for a hand run outside the tick.
DEFAULT_HOME = "/opt/data"

ENABLED_ENV = "FEEDBACK_PROMPT_ENABLED"
DELAY_ENV = "FEEDBACK_PROMPT_DELAY"

# Only an explicit "off" disables; a typo in the value leaves the default on,
# which is the direction a feedback prompt should fail in.
FALSE_VALUES = frozenset({"0", "false", "no", "off"})

DELAY_RE = re.compile(r"^(\d+)([dhm])$")
UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60}
DEFAULT_DELAY = "7d"
DEFAULT_DELAY_SECONDS = 7 * UNIT_SECONDS["d"]
# Slack for a delay of at least a day, and none below that; see the module
# docstring. Ten minutes is well above the sub-minute ticker phase plus Hermes
# start-up that separate one day's tick from the next, and far below the day
# between ticks, so it cannot bring the message forward by a tick.
DUE_SLACK_SECONDS = 10 * 60

ARMED_MARKER = ".feedback_prompt_armed"
SENT_MARKER = ".feedback_prompt_sent"
MARKER_MODE = 0o644

# This job's own id and the scheduler's store for the profile, relative to
# HERMES_HOME; the store's shape is what `chat_delivery_watch.py` reads.
JOB_ID = "feedback-prompt"
STORE_PATH = Path("cron") / "jobs.json"
LAST_RUN_AT_FIELD = "last_run_at"
LAST_DELIVERY_ERROR_FIELD = "last_delivery_error"
# Attempts in all, the first included: two weeks of daily ticks. Each one is a
# Chat Agent composition, and an install that has bound no chat platform after
# two weeks of tries is not one this message can reach.
MAX_ATTEMPTS = 14
FIRST_ATTEMPT = 1

LOG_PREFIX = "feedback_prompt"

# Always the short link, never a Google Forms URL: `scripts/feedback_form/README.md`
# records that the docs site serves this as a redirect to wherever the form is.
FORM_URL = "https://gke-labs.github.io/kube-agents/feedback"
REPOSITORY = "gke-labs/kube-agents"

# `deliver: "chat"` relays this through a Chat Agent turn. A heading keeps the
# message reading as a report to reproduce rather than a greeting to answer.
HEADING = "How is kube-agents working out?"
MESSAGE = f"""{HEADING}

kube-agents has been running here for a while now, and the maintainers would like to hear how it has gone: what has been useful, what has got in the way, and what is missing. The feedback form takes a couple of minutes and needs no GitHub or Google account: {FORM_URL}

A submission becomes a public issue on {REPOSITORY}, apart from the form's optional follow-up email, so keep cluster names, project ids and anything read from a Secret out of it. A reply in this thread reaches the agent like any other message.
"""


def _home() -> Path:
    return Path(os.environ.get(HOME_ENV, DEFAULT_HOME))


def enabled(value: str | None) -> bool:
    """The on/off knob. Unset and anything but an explicit false value is on."""
    if value is None:
        return True
    return value.strip().lower() not in FALSE_VALUES


def parse_delay(value: str | None) -> int:
    """The delay in seconds, from ``<n>d|h|m``; the default when unset.

    Raises ``ValueError`` for a value that does not parse. The caller turns that
    into a failed run rather than a fallback, so the misconfiguration is seen.
    """
    if value is None or not value.strip():
        return DEFAULT_DELAY_SECONDS
    match = DELAY_RE.match(value.strip().lower())
    if match is None:
        raise ValueError(f"{DELAY_ENV}={value!r} is not <n>d, <n>h or <n>m (the default is {DEFAULT_DELAY})")
    return int(match.group(1)) * UNIT_SECONDS[match.group(2)]


def due(anchored_at: float, now: float, delay: int) -> bool:
    """Whether the delay has elapsed, with slack for a delay the daily tick quantises."""
    slack = DUE_SLACK_SECONDS if delay >= UNIT_SECONDS["d"] else 0
    return now - anchored_at >= delay - slack


def _create_exclusive(path: Path, content: str) -> bool:
    """Create ``path`` holding ``content`` unless it exists. True if this call created it.

    ``O_CREAT | O_EXCL`` is one filesystem operation, so "does it exist?" and "it
    exists now, and it is mine" are indivisible. Raises ``OSError`` for anything
    other than the file already being there.
    """
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, MARKER_MODE)
    except FileExistsError:
        return False
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)
    return True


def anchor(home: Path, now: float) -> float | None:
    """The anchor time, arming on the first call.

    Returns ``None`` when this call armed the prompt: the clock starts now and
    there is nothing else to do this tick. Otherwise the time recorded in the
    marker, or the marker's mtime when its content is unreadable, so a marker
    somebody truncated by hand still anchors rather than re-arming forever.
    """
    marker = home / ARMED_MARKER
    if _create_exclusive(marker, f"{now:.0f}\n"):
        return None
    try:
        return float(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return marker.stat().st_mtime


def claim_record(attempted_at: float, attempts: int) -> str:
    """What the sent marker holds: the latest attempt's time, then the count."""
    return f"{attempted_at:.0f}\n{attempts}\n"


def parse_claim(text: str) -> tuple[float, int] | None:
    """The latest attempt's time and the count from a sent marker.

    ``None`` when the first line is not a time, which the caller reads as "sent,
    and not by a record this script can reason about": no retry. A marker with
    no count line was written before retries existed and counts as one attempt.
    """
    lines = text.split("\n")
    try:
        attempted_at = float(lines[0].strip())
    except ValueError:
        return None
    try:
        attempts = int(lines[1].strip())
    except (IndexError, ValueError):
        attempts = FIRST_ATTEMPT
    return attempted_at, attempts


def delivery_failed(store: Path, attempted_at: float) -> bool:
    """Whether the scheduler recorded the run that carried the attempt as undelivered.

    True only on positive evidence: the store parses, carries this job, its
    ``last_run_at`` (stamped after delivery) is at or after the attempt, and its
    ``last_delivery_error`` grades as a hard failure (nothing reached any
    platform). A delivered note, a partial or degraded delivery, a record of an
    earlier run, or no readable record is False: the message may have landed,
    and posting it twice costs more than a lost retry.
    """
    try:
        data = json.loads(store.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return False
    for job in jobs:
        if not isinstance(job, dict) or job.get("id") != JOB_ID:
            continue
        recorded_at = parse_iso(job.get(LAST_RUN_AT_FIELD))
        if recorded_at is None or recorded_at.timestamp() < attempted_at:
            return False
        error = job.get(LAST_DELIVERY_ERROR_FIELD)
        if not isinstance(error, str) or not error:
            return False
        return not is_delivered_note(error) and grade_error(error) == GRADE_HARD
    return False


def _claim_retry(marker: Path, store: Path, now: float) -> bool:
    """Take the next attempt when the last one is recorded as undelivered. True if taken.

    The decision and the rewrite happen under an exclusive lock on the marker,
    so of two runs racing on the retry exactly one rewrites it; the other then
    reads an attempt time the store has no record at or after, and stays
    silent. The lock dies with the process, so a crash leaves nothing behind
    that could wedge the next tick. Raises ``OSError`` when the marker cannot
    be opened or locked.
    """
    with open(marker, "r+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        claim = parse_claim(handle.read())
        if claim is None:
            return False
        attempted_at, attempts = claim
        if attempts >= MAX_ATTEMPTS or not delivery_failed(store, attempted_at):
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(claim_record(now, attempts + 1))
        handle.flush()
    return True


def main(home: Path | None = None, now: float | None = None) -> int:
    if not enabled(os.environ.get(ENABLED_ENV)):
        return 0  # silent, and nothing on disk changes
    # Before arming: a run that cannot read its own configuration should not
    # start the clock, and should fail where the failure is reported.
    try:
        delay = parse_delay(os.environ.get(DELAY_ENV))
    except ValueError as exc:
        sys.stderr.write(f"{LOG_PREFIX}: {exc}\n")
        return 1
    if home is None:
        home = _home()
    if now is None:
        now = time.time()

    try:
        anchored_at = anchor(home, now)
    except OSError as exc:
        sys.stderr.write(f"{LOG_PREFIX}: cannot write {home / ARMED_MARKER}: {exc}\n")
        return 1
    if anchored_at is None:
        return 0  # armed this tick

    if not due(anchored_at, now, delay):
        return 0  # not due yet

    # The claim decides; nothing reaches stdout before it succeeds. A marker
    # already there is a predecessor's or a racing run's attempt: retry only
    # when the scheduler recorded that attempt as undelivered.
    marker = home / SENT_MARKER
    try:
        claimed = _create_exclusive(marker, claim_record(now, FIRST_ATTEMPT))
        if not claimed:
            claimed = _claim_retry(marker, home / STORE_PATH, now)
    except OSError as exc:
        sys.stderr.write(f"{LOG_PREFIX}: cannot write {marker}: {exc}\n")
        return 1
    if not claimed:
        return 0  # delivered, or being retried by a racing run, or out of attempts

    sys.stdout.write(MESSAGE)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
