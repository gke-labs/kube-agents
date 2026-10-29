#!/usr/bin/env python3
"""Post the weekly health summary to the CI-health Chat space, once a week.

weekly_email.py writes the summary (``--chat-out``: ``{week_end, text,
card}``) when the week's page passes every integrity check; a refused week writes
none, so there is nothing to post. This script posts it on the first tick at
or after POST_HOUR on the Monday after the week ends, Toronto time (9 AM
ET), and records the week in a small state object (``--state``, a local path
or a gs:// object, this script's only write) so a later tick, or a re-run of
the same tick, posts nothing for that week. A Monday missed is retried
each tick until the summary moves to the next week (weekly_email.py's week
ends the Sunday before today, UTC: Sunday 7 or 8 PM Toronto).

It posts the card (cardsV2) with the text as its fallback, or the text alone
when the summary has no card or Chat refuses the card (HTTP 400: nothing was
posted), the way post_health.py posts: the Smoke Health app's token in
CI_HEALTH_CHAT_TOKEN and the space in CI_HEALTH_CHAT_SPACE (or the webhook
alternative), through post_health.Sender. CI_HEALTH_MUTE=true posts nothing
and records nothing, so a week muted on Monday posts once unmuted, within
the week. A state object it cannot read (anything but a NotFound, or not
JSON) posts nothing that tick, rather than risk posting a week twice. The
week is recorded before the post, so it goes out at most once: a record that
fails to write posts nothing (the next tick retries), and a post that fails
after the record is written is not retried -- the week is missed, and the
log says so.

Run:  python3 scripts/eval_dashboard/weekly_chat.py --message weekly-chat.json --state weekly-chat-state.json --dry-run
Test: cd scripts && python3 -m unittest test_eval_dashboard_weekly_chat
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.request

try:
    from eval_dashboard import post_health
    from eval_dashboard.weekly_email import CHAT_CARD_KEY, CHAT_TEXT_KEY, CHAT_WEEK_KEY
except ImportError:  # run as a script: scripts/eval_dashboard/weekly_chat.py
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from eval_dashboard import post_health
    from eval_dashboard.weekly_email import CHAT_CARD_KEY, CHAT_TEXT_KEY, CHAT_WEEK_KEY

# The send time: the Monday after the week's last night (a Sunday), at this
# hour on the reader's clock (post_health.LOCAL_TZ, Toronto), the digest's
# hour, so the week's summary follows Monday's digest.
POST_DAYS_AFTER_WEEK_END = 1
POST_HOUR = 9

MUTE_ENV = "CI_HEALTH_MUTE"
MUTE_ON = "true"
STATE_WEEK_KEY = "week_end"
STATE_POSTED_KEY = "posted_at"
# `gcloud storage cat`'s words for an object that does not exist ("One or
# more URLs matched no objects", as collect.py reads them); any other failure
# is not "never posted".
NOT_FOUND_RE = re.compile(r"matched no objects|NotFound|404")
# Chat refusing the card itself: nothing was posted, so the text alone goes
# out instead, still once.
HTTP_BAD_REQUEST = 400


def log(message: str) -> None:
    print(message, file=sys.stderr)


def send_time(week_end: datetime.date, tz=post_health.LOCAL_TZ) -> datetime.datetime:
    day = week_end + datetime.timedelta(days=POST_DAYS_AFTER_WEEK_END)
    return datetime.datetime.combine(day, datetime.time(POST_HOUR), tzinfo=tz)


def read_posted(location: str, runner=subprocess.run) -> tuple[dict, bool]:
    """``(state, read)``: the last posted week, ``{}`` when none is on
    record; ``read`` is False when the object exists but could not be read."""
    if location.startswith(post_health.GS_PREFIX):
        result = runner([post_health.GCLOUD, post_health.STORAGE_GROUP, "cat", location], capture_output=True, text=True)
        if result.returncode != 0:
            return {}, bool(NOT_FOUND_RE.search(result.stderr or ""))
        text = result.stdout
    else:
        path = pathlib.Path(location)
        if not path.exists():
            return {}, True
        try:
            text = path.read_text()
        except OSError:
            return {}, False
    try:
        loaded = json.loads(text)
    except ValueError:
        return {}, False
    return (loaded, True) if isinstance(loaded, dict) else ({}, False)


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--message", required=True, type=pathlib.Path, help="the summary weekly_email.py --chat-out wrote")
    parser.add_argument("--state", required=True, help="the last posted week: local path or gs:// object (this script's only write)")
    parser.add_argument("--now", help="evaluate as of this ISO 8601 time (default: now)")
    parser.add_argument("--dry-run", action="store_true", help="print the message body instead of posting; records nothing")
    return parser.parse_args(argv)


def main(argv=None, environ=os.environ, opener=urllib.request.urlopen, runner=subprocess.run) -> int:
    args = parse_args(argv)
    if environ.get(MUTE_ENV) == MUTE_ON:
        log(f"{MUTE_ENV}={MUTE_ON}: the weekly summary is muted; nothing posted")
        return 0
    try:
        message = json.loads(args.message.read_text())
        week_end = datetime.date.fromisoformat(message[CHAT_WEEK_KEY])
        text = message[CHAT_TEXT_KEY]
        card = message.get(CHAT_CARD_KEY)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log(f"no weekly summary to post ({args.message}: {exc}); the email was not rendered this tick")
        return 0
    now = post_health.parse_iso(args.now) or datetime.datetime.now(post_health.UTC)
    due = send_time(week_end)
    if now < due:
        log(f"weekly summary for the week ending {week_end}: not due until {post_health.clock(due, weekday=True)}")
        return 0
    state, read = read_posted(args.state, runner)
    if not read:
        log(f"could not read {args.state}; not posting this tick, so no week is posted twice")
        return 1
    if state.get(STATE_WEEK_KEY) == week_end.isoformat():
        log(f"weekly summary for the week ending {week_end}: already posted at {state.get(STATE_POSTED_KEY)}")
        return 0
    if args.dry_run:
        print(text if card is None else json.dumps(post_health.Sender.body(text, card), indent=2, ensure_ascii=False))
        return 0
    sender = post_health.Sender.from_env(environ, opener)
    if not sender.configured:
        log(post_health.NOT_CONFIGURED)
        return 0
    try:
        post_health.write_state(args.state, {STATE_WEEK_KEY: week_end.isoformat(),
                                             STATE_POSTED_KEY: now.isoformat(timespec="seconds")}, runner)
    except (OSError, subprocess.CalledProcessError) as exc:
        log(f"weekly summary for the week ending {week_end}: {args.state} could not be written ({exc}); not posting, the next tick retries")
        return 1
    sent = sender.send(text, card)
    if not sent and card is not None and sender.status == HTTP_BAD_REQUEST:
        log(f"weekly summary for the week ending {week_end}: Chat refused the card (HTTP {HTTP_BAD_REQUEST}); posting the text instead")
        sent = sender.send(text)
    if not sent:
        log(f"ERROR: weekly summary for the week ending {week_end}: the post failed after the week was recorded in "
            f"{args.state}; it is NOT retried and this week's summary is missed (delete the object to post it again)")
        return 1
    log(f"weekly summary for the week ending {week_end}: posted via {sender.describe()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
