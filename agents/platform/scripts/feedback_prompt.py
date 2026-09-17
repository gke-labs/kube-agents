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
- ``.feedback_prompt_sent`` is the claim. It is created with ``O_CREAT |
  O_EXCL`` *before* anything reaches stdout, so of two runs racing on the same
  home (a scheduled tick and an on-demand run, say) exactly one wins the create
  and prints; the loser exits silently. Checking the marker and writing it
  after printing would leave both runs inside the same window, and the channel
  would get the request twice. It holds the time of the claim, for a person
  reading it; nothing reads it back, so an emptied marker is still a claim.

Printing is not delivering. The scheduler relays the output after the run and
writes the outcome into this job's entry in the profile's ``cron/jobs.json``
as ``last_delivery_error``, the record ``chat_delivery_watch.py`` grades. This
script does not read that record back to repeat a post it graded as a
failure, because the grade cannot tell a post that landed from one that did
not: a ``hermes send`` that posts, exits 0 and prints no readable message id
is recorded as ``composed but not delivered``, the same words as a send that
failed, and so is a relay that raises or times out after the post has gone
out. A retry on that record posts the request twice, and once it is in a
channel nothing takes it back; a post the scheduler recorded as undelivered
(the relay or the platform was down on the due tick, or no chat platform was
bound yet) is a lost request, which the ledger ``chat-delivery-watch`` keeps
names like any other failed delivery, and which an operator sends again by
removing ``.feedback_prompt_sent`` from the profile home: the next tick claims
afresh. At most once is the direction the job fails in. One Chat Agent
composition is all it ever costs, on the one tick that prints; every other
tick prints nothing and relays nothing.

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

import errno
import math
import os
import re
import sys
import time
from pathlib import Path

HOME_ENV = "HERMES_HOME"
# The markers live in the platform profile's home, which is what the ticker
# sets HERMES_HOME to for this roster: `profiles/platform` under the data
# volume (`profile_cron_tick.py`). The container itself sets HERMES_HOME to the
# volume's root, the gateway's home (`docker-entrypoint.sh`), so a hand run in
# the container inherits that and would otherwise arm and claim a second pair
# of markers there and see none of the scheduled ticks' state; `_home` resolves
# the profile home under whichever of the two it is handed.
GATEWAY_HOME = "/opt/data"
PROFILE_HOME = Path("profiles") / "platform"

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
    """The profile home: HERMES_HOME itself, or the platform profile under it.

    Under the ticker HERMES_HOME is the profile home and has no profile of its
    own beneath it; in a shell in the container it is the gateway's home, and
    the platform profile is a directory under it.
    """
    home = Path(os.environ.get(HOME_ENV, GATEWAY_HOME))
    profile_home = home / PROFILE_HOME
    return profile_home if profile_home.is_dir() else home


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


def stamp(at: float) -> str:
    """A time as the whole second it fell in, the form both markers hold it in.

    Floored, never rounded, so a marker never reads later than the run that
    wrote it; the scheduler stamps the run's record a fraction of a second
    after this script's clock.
    """
    return str(math.floor(at))


def _create_exclusive(path: Path, content: str) -> bool:
    """Create ``path`` holding ``content`` unless it exists. True if this call created it.

    ``O_CREAT | O_EXCL`` is one filesystem operation, so "does it exist?" and "it
    exists now, and it is mine" are indivisible. Raises ``OSError`` for anything
    other than the file already being there. A create that succeeds and a
    payload that does not (the volume full, say) removes the file again before
    raising: an empty marker left behind would read on every later tick as a
    claim, after a run that printed nothing, and the prompt would be lost for
    good.
    """
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, MARKER_MODE)
    except FileExistsError:
        return False
    data = content.encode("utf-8")
    try:
        try:
            written = os.write(fd, data)
        finally:
            os.close(fd)
        if written != len(data):
            raise OSError(errno.ENOSPC, f"{os.strerror(errno.ENOSPC)}: wrote {written} of {len(data)} bytes", str(path))
    except OSError:
        path.unlink(missing_ok=True)
        raise
    return True


def anchor(home: Path, now: float) -> float | None:
    """The anchor time, arming on the first call.

    Returns ``None`` when this call armed the prompt: the clock starts now and
    there is nothing else to do this tick. Otherwise the time recorded in the
    marker, or the marker's mtime when its content is unreadable, so a marker
    somebody truncated by hand still anchors rather than re-arming forever.
    """
    marker = home / ARMED_MARKER
    if _create_exclusive(marker, f"{stamp(now)}\n"):
        return None
    try:
        return float(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return marker.stat().st_mtime


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
    # already there is a predecessor's or a racing run's claim, and the post
    # is theirs whatever the scheduler recorded of it.
    marker = home / SENT_MARKER
    try:
        claimed = _create_exclusive(marker, f"{stamp(now)}\n")
    except OSError as exc:
        sys.stderr.write(f"{LOG_PREFIX}: cannot write {marker}: {exc}\n")
        return 1
    if not claimed:
        return 0  # sent, by this run's predecessor or a racing one

    sys.stdout.write(MESSAGE)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
