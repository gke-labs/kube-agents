#!/usr/bin/env python3
"""The findings nudge: names findings from the queue in chat, paced.

Backs the ``findings-morning-nudge`` cron job, which runs hourly with
``no_agent: true`` and ``deliver: "chat"``. Its stdout is delivered verbatim,
so everything this prints is what the user reads, and printing nothing relays
nothing. Most hours have nothing to say.

No model turn, because there is no judgement to make: the ordering is the
queue's (`GET /v1/findings/ranked`), what may be named now is
``findings_queue.pace`` (§7.2 of `docs/designs/inventory-findings-queue.md`),
and the recommendation was written by whoever found the thing. What is left is
formatting and bookkeeping. Each run:

1. Expires lapsed snoozes, since this job is the queue's only tick.
2. Reads the open queue and the items already added today, and asks ``pace``
   what to name. New criticals are added from 12:00 UTC, at most
   ``FINDINGS_DAILY_CRITICALS`` a day; non-criticals from
   ``FINDINGS_NONCRITICAL_AFTER_HOUR`` UTC, at most ``FINDINGS_NONCRITICAL_MAX``
   a day, and only while no critical is pending or waiting. While any
   non-critical the nudge named waits for a decision, nothing is added.
3. Once a UTC day, on the first run at or after 12:00 UTC, also reminds the
   top pending criticals and names every pending non-critical holding
   additions back, with how to release it. A decision on any id of an item
   covers every undecided row of the item (`findings_queue.patch_finding`),
   and the count printed beside the id is that number of objects. Shown rows
   a complete sweep stopped reporting are in that count but not listed, and
   the line says how many of them there are.
4. Prints, records the day's state (the daily part sent, the items announced
   today) in a file in the profile home, then marks every named row surfaced
   as the paced publisher, which is what makes an addition pending and
   records it against the day. An announced item counts against the day from
   the state file alone while its marks have not landed.

Nothing is added before the first inventory report's delivery is claimed, so
that report is the first thing onboarding says about the fleet. The hold ends
``FIRST_REPORT_HOLD_HOURS`` (24) after onboarding filed its sweep, delivered or
not: an undelivered report by then means onboarding stalled, and holding on
would keep the queue silent for good. Each run that is held, or would be but
for that limit, says which on stderr. A run that cannot read the queue, or
fails in any other way before it prints, fails (exit 1, which the scheduler
posts as a failure) once a UTC day; later failures that day go to stderr and
exit 0.

The bookkeeping is best-effort, and what a failed write costs: an addition
whose mark fails is not offered again the same day (the state file remembers
it), still counts against that day's limit, and holds additions back as a
pending item of its class would, though the stop-add notice does not name it.
Unlike a recorded addition, it counts only while undecided: a decision on it
the same day refunds its slot. It is offered as new the next day. If the state file cannot be written
either, the same additions and the daily part are posted again every hour.
"""

import http.client
import json
import os
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import findings_queue as fq

DEFAULT_ENDPOINT = "http://127.0.0.1:8699"
TIMEOUT_SECONDS = 30

PUBLISHER = "nudge"

# `deliver: "chat"` relays this through a Chat Agent turn. Without a heading a
# short message reads as conversation rather than as a report to reproduce.
HEADING = "Findings queue"

# A gathered item lists this many of its other objects by name, then counts
# the rest; a fleet-wide condition can cover dozens.
MAX_LISTED_MEMBERS = 5

# The profile home, where the once-a-day state lives, and the gateway home,
# where onboarding's markers live. Under the ticker HERMES_HOME is the profile
# home (`profiles/platform`); in a shell in the container it is the gateway's.
HOME_ENV = "HERMES_HOME"
GATEWAY_HOME = "/opt/data"
PROFILE_HOME = Path("profiles") / "platform"
PROFILES_DIR = "profiles"
STATE_FILE = ".findings_nudge_state.json"
DAILY_KEY = "daily_section"
FAILURE_KEY = "failure_reported"
# {"day": <UTC date>, "keys": [<item key>, ...]}: what was added today, so a
# mark that failed does not announce the same item again an hour later, and
# the item still counts against the day and holds additions back (`pace`).
ANNOUNCED_KEY = "announced"
# The run id sent with an addition's marks: one per run, so the members of an
# item marked one by one are one addition.
RUN_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Onboarding's markers (`bootstrap_scan_gate.py`, `bootstrap_delivery.py`).
# Filed and not yet delivered means a first report is on its way.
SCAN_FILED_MARKER = ".bootstrap_scan_filed"
DELIVERED_MARKER = ".bootstrap_completed"
# The line of the filed marker holding when it was filed, in Unix seconds.
# The marker's mtime stands in when the line is missing or unreadable.
FILED_AT_PREFIX = "filed_at="
# How long after the sweep was filed the first report is waited for. A report
# still undelivered by then means onboarding stalled, and holding on would add
# nothing ever again.
FIRST_REPORT_HOLD_HOURS = 24
FIRST_REPORT_HOLD = timedelta(hours=FIRST_REPORT_HOLD_HOURS)
TIME_FORMAT = "%Y-%m-%d %H:%M UTC"

LOG_PREFIX = "findings_nudge"


def _request(endpoint: str, path: str, body: dict | None = None, method: str = "") -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    token = (os.environ.get("SESSION_KV_API_KEY") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{endpoint}{path}", data=data, headers=headers, method=method or ("POST" if data else "GET")
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _homes() -> tuple[Path, Path]:
    """(gateway home, profile home), from whichever of the two HERMES_HOME is."""
    home = Path(os.environ.get(HOME_ENV) or GATEWAY_HOME)
    if (home / PROFILE_HOME).is_dir():
        return home, home / PROFILE_HOME
    if home.parent.name == PROFILES_DIR:
        return home.parent.parent, home
    return home, home


def _filed_at(marker: Path) -> tuple[datetime, str] | None:
    """When the sweep was filed, and where that came from; None once the marker is gone."""
    try:
        for line in marker.read_text(encoding="utf-8").splitlines():
            if line.startswith(FILED_AT_PREFIX):
                seconds = int(line[len(FILED_AT_PREFIX):].strip())
                return datetime.fromtimestamp(seconds, timezone.utc), f"its {FILED_AT_PREFIX} line"
    except (OSError, ValueError, OverflowError):
        pass
    try:
        mtime = marker.stat().st_mtime
    except OSError:
        # Removed since the caller saw it, as the re-arm runbook does.
        return None
    return datetime.fromtimestamp(mtime, timezone.utc), "its mtime"


def first_report_hold(gateway_home: Path, now: datetime) -> tuple[bool, str]:
    """Whether to add nothing yet, and the line to log about it ("" for none).

    Held while onboarding has filed its sweep, has not delivered the report,
    and filed it no more than `FIRST_REPORT_HOLD` ago. An install where
    onboarding never started (the experimental front door does not run it)
    has neither marker and is not held.
    """
    filed, delivered = gateway_home / SCAN_FILED_MARKER, gateway_home / DELIVERED_MARKER
    if not filed.exists() or delivered.exists():
        return False, ""
    found = _filed_at(filed)
    if found is None:
        return False, ""
    filed_at, source = found
    markers = f"{filed} present, filed {filed_at.strftime(TIME_FORMAT)} by {source}; {delivered} absent"
    if now - filed_at > FIRST_REPORT_HOLD:
        return False, (
            f"{LOG_PREFIX}: the first inventory report is still undelivered more than "
            f"{FIRST_REPORT_HOLD_HOURS} hours after its sweep was filed, so additions are no longer "
            f"held for it ({markers})"
        )
    return True, (
        f"{LOG_PREFIX}: adding nothing until the first inventory report's delivery is claimed, or until "
        f"{(filed_at + FIRST_REPORT_HOLD).strftime(TIME_FORMAT)} ({markers})"
    )


def _load_state(home: Path) -> dict:
    try:
        state = json.loads((home / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _save_state(home: Path, state: dict) -> bool:
    path = home / STATE_FILE
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        sys.stderr.write(f"{LOG_PREFIX}: could not record its state in {path}: {exc}\n")
        return False
    return True


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _where(finding: dict) -> str:
    cluster = finding.get("cluster") or ""
    # The project leads: a cluster name alone is ambiguous once two projects
    # are in scope, and this line is where the reader goes to look.
    # A cluster-scoped finding names the cluster as its object, and reading
    # `prod/prod` back is a puzzle rather than a location.
    parts = [finding.get("project") or "", cluster, finding.get("namespace") or "", finding.get("object") or ""]
    if parts[3] == cluster:
        parts = parts[:3]
    return "/".join(part for part in parts if part)


def _object(finding: dict) -> str:
    """A member's place within its item, whose project and cluster it shares."""
    return "/".join(part for part in (finding.get("namespace") or "", finding.get("object") or "") if part)


def _item_lines(items: list, start: int = 1) -> list[str]:
    lines = []
    for position, item in enumerate(items, start=start):
        top, others = item.members[0], item.members[1:]
        action = (top.get("recommendation") or {}).get("action") or ""
        entry = f"{position}. [{top.get('rank_score')}] {top.get('title')}\n   {_where(top)}"
        if others:
            listed = ", ".join(_object(row) for row in others[:MAX_LISTED_MEMBERS])
            unlisted = len(others) - min(len(others), MAX_LISTED_MEMBERS)
            entry += f"\n   also {listed}" + (f" and {unlisted} more" if unlisted else "")
        if action:
            entry += f"\n   {action}"
        entry += f"\n   id: {top.get('id')}"
        if item.covers > 1:
            entry += f" (a decision on it covers all {item.covers} objects in this item"
            # The rest are shown rows a complete sweep stopped reporting: no
            # longer pending, so not listed, but still decided with the item.
            unreported = item.covers - len(item.members)
            if unreported:
                entry += f", {unreported} of them not listed because the last complete sweep did not report them"
            entry += ")"
        lines.append(entry)
    return lines


def compose(plan: fq.PacingPlan, open_count: int, daily: bool) -> str:
    """The message, or "" when there is nothing to say. Pure, so the wording earns a test.

    `daily` is whether this run carries the once-a-day part: the reminders and
    the stop-add notice.
    """
    critical = fq.ITEM_CLASSES[0]
    sections = []
    added_critical = [item for item in plan.add if item.item_class == critical]
    added_other = [item for item in plan.add if item.item_class != critical]
    if added_critical:
        sections.append(
            "\n".join([f"New: {_plural(len(added_critical), 'critical finding', 'critical findings')}."]
                      + _item_lines(added_critical))
        )
    if added_other:
        sections.append(
            "\n".join([f"New: {_plural(len(added_other), 'finding', 'findings')}, none of them critical."]
                      + _item_lines(added_other))
        )
    if daily and plan.remind:
        sections.append(
            "\n".join(
                [f"Still open: {_plural(len(plan.remind), 'critical finding', 'critical findings')}, undecided."]
                + _item_lines(plan.remind)
            )
        )
    if daily and plan.blocking:
        waiting_critical = sum(1 for item in plan.waiting if item.item_class == critical)
        waiting_other = len(plan.waiting) - waiting_critical
        counts = []
        if waiting_critical:
            counts.append(_plural(waiting_critical, "critical finding", "critical findings"))
        if waiting_other:
            kind = "other finding" if waiting_critical else "finding"
            counts.append(_plural(waiting_other, kind, f"{kind}s"))
        verb = "is" if len(plan.waiting) == 1 else "are"
        waiting = f" {' and '.join(counts)} {verb} waiting." if counts else ""
        sections.append(
            "\n".join(
                [
                    "New findings are on hold until "
                    + ("this one is" if len(plan.blocking) == 1 else f"these {len(plan.blocking)} are")
                    + " dismissed, snoozed or accepted." + waiting
                ]
                + _item_lines(plan.blocking)
                + [
                    "Reply with the id and the decision: dismiss (won't fix), snooze until a "
                    "date, or accept (being worked on). "
                    "A decision on an id covers every undecided object in its item."
                ]
            )
        )
    if not sections:
        return ""

    tail = f"{_plural(open_count, 'finding is', 'findings are')} open on the queue. Ask for the full list."
    if plan.rolled_up:
        tail += (
            f" {_plural(plan.rolled_up, 'of them is', 'of them are')} provider-managed,"
            " with no next step you can take, never named here."
        )
    return f"{HEADING}\n\n" + "\n\n".join(sections) + f"\n\n{tail}"


def _report_failure(home: Path, state: dict, today: str) -> int:
    """Exit 1 once a UTC day, so an outage is one failure post rather than one an hour."""
    if state.get(FAILURE_KEY) == today:
        sys.stderr.write(f"{LOG_PREFIX}: already reported a failure today; not posting another\n")
        return 0
    state[FAILURE_KEY] = today
    _save_state(home, state)
    return 1


def _mark(endpoint: str, item, added_class: str = "", run: str = "") -> None:
    # After the message, and best-effort: a failure costs the bookkeeping, not
    # the message, which is already out. An addition that is not marked is
    # offered again on a later day.
    body = {"publisher": PUBLISHER}
    if added_class:
        body["added_class"] = added_class
        body["run"] = run
    for row in item.members:
        try:
            _request(endpoint, f"/v1/findings/{urllib.parse.quote(str(row['id']), safe='')}/surfaced", body)
        except Exception as exc:  # one bad mark must not skip the rest
            sys.stderr.write(f"{LOG_PREFIX}: could not mark {row.get('id')} surfaced: {exc}\n")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def main(argv: list[str] | None = None, clock=_now) -> int:
    now = clock().astimezone(timezone.utc)
    today = now.date().isoformat()
    endpoint = (os.environ.get("SESSION_KV_ENDPOINT") or DEFAULT_ENDPOINT).rstrip("/")
    gateway_home, home = _homes()
    state = _load_state(home)

    # Before the read, so a snooze that lapsed since the last run is back on
    # the list this run reads. Best-effort: a failed expiry costs an hour of
    # lateness for snoozed rows, not the message.
    try:
        _request(endpoint, "/v1/findings/expire-snoozes", {})
    except Exception as exc:
        sys.stderr.write(f"{LOG_PREFIX}: could not expire lapsed snoozes: {exc}\n")

    try:
        rows = _request(endpoint, "/v1/findings/ranked").get("findings") or []
        added_today = _request(endpoint, f"/v1/findings/additions?day={today}")
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError, AttributeError) as exc:
        detail = exc.read().decode("utf-8", "replace") if isinstance(exc, urllib.error.HTTPError) else str(exc)
        # Nothing on stdout: a run that could not read the queue must not read
        # as a quiet hour.
        sys.stderr.write(f"{LOG_PREFIX}: could not read the queue at {endpoint}: {detail}\n")
        return _report_failure(home, state, today)

    # Anything else that breaks before the message is out is also one failure
    # post a day, not one an hour.
    try:
        held, why = first_report_hold(gateway_home, now)
        if why:
            sys.stderr.write(why + "\n")
        announced = state.get(ANNOUNCED_KEY) if isinstance(state.get(ANNOUNCED_KEY), dict) else {}
        announced_keys = (announced.get("keys") or []) if announced.get("day") == today else []
        plan = fq.pace(rows, now, fq.pacing_limits(os.environ), added_today, may_add=not held, announced=announced_keys)
        daily = now.hour >= fq.REMIND_HOUR and state.get(DAILY_KEY) != today
        open_count = len({fq.item_key(row) for row in rows if not fq.rolled_up(row)}) + plan.rolled_up
        message = compose(plan, open_count, daily)
    except Exception:
        sys.stderr.write(f"{LOG_PREFIX}: failed before posting:\n{traceback.format_exc()}")
        return _report_failure(home, state, today)

    if message:
        sys.stdout.write(message + "\n")
        sys.stdout.flush()

    # Before the marks, so a mark that raises cannot cost the record that the
    # message went out.
    if plan.add:
        state[ANNOUNCED_KEY] = {"day": today, "keys": announced_keys + [list(item.key) for item in plan.add]}
    if daily:
        state[DAILY_KEY] = today
    if plan.add or daily:
        _save_state(home, state)

    run = now.strftime(RUN_FORMAT)
    for item in plan.add:
        _mark(endpoint, item, item.item_class, run)
    if daily:
        # Marking a reminded or blocking item also shows any member that joined
        # it since, so that member is pending with the rest of its line.
        for item in plan.remind + plan.blocking:
            _mark(endpoint, item)
    return 0


if __name__ == "__main__":
    sys.exit(main())
