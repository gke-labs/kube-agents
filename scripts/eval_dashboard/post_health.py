#!/usr/bin/env python3
"""Post the gate's health to Google Chat -- on state changes, plus one digest a day.

health.py decides GREEN / DEGRADED / OUTAGE every tick; this is the half that
tells people, and its whole design is about NOT telling them most of the
time. It reads the current health.json and the state it last posted, and
sends a message only when:

    the state changed                       -> "CI health: DEGRADED (was GREEN)"
    the state returned to GREEN             -> the recovery, with how long it lasted
    an OUTAGE grew to name a new case       -> the same shape, rate-limited
    it is the digest hour and none went out -> the daily digest with the 24h numbers

Everything else is silence. The last-posted state lives in a small JSON file
(`--state`, a local path or a gs:// object) that this script is the only
writer of; the scheduled job is otherwise stateless.

A fourth message, rarer than the others: when health.json reports that
data.json itself has stopped refreshing (`stale`), the space is told once,
and once more when it resumes -- a silent stall would otherwise freeze the
state and keep the digest reporting old numbers as current.

Every time a reader sees is on the reader's clock: America/Toronto, written
"7:30 AM ET", never UTC (the deep links and the state file keep ISO UTC).
The digest hour is a Toronto hour too, and "once a day" is a Toronto day.

One side effect beyond posting: on a new OUTAGE with no tracking issue --
none in case-notes.yaml, none open under the `presubmit-gate` label naming
the same cases -- gate_issue.py files one with the workflow's GitHub token
(`gh api`, GH_TOKEN), the "broken" message says "Tracking #NNN", and the
recovery comments on it. It never closes an issue. A GitHub failure is a
warning: the message goes out with "no issue yet" and the next change asks
again.

Delivery is the Google Chat REST API with the job's service account acting
as a Chat app: POST https://chat.googleapis.com/v1/{space}/messages with an
OAuth token bearing the chat.bot scope (the workflow mints one with `gcloud
auth print-access-token --scopes=...` and passes it in CI_HEALTH_CHAT_TOKEN;
incoming webhooks are disabled org-wide). The space id comes from
CI_HEALTH_CHAT_SPACE (`spaces/XXXX`, not a secret). An incoming-webhook URL
in CI_HEALTH_CHAT_WEBHOOK is the optional alternative, used when the space
and token are not both present. Neither the token nor the webhook URL is
ever printed: a failure logs the HTTP status and nothing from the request.
With nothing configured the script says so and exits 0 -- the job must not
fail while the space is being set up.

Run:  python3 scripts/eval_dashboard/post_health.py --health health.json --state state.json --dry-run
Test: cd scripts && python3 -m unittest test_eval_dashboard_post_health
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    from eval_dashboard import gate_issue, ghcli
except ImportError:  # run as a script: scripts/eval_dashboard/post_health.py
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from eval_dashboard import gate_issue, ghcli

STATE_SCHEMA_VERSION = 1

# The reader's clock. Every time rendered into a message is converted here
# and written "7:30 AM ET"; DST is zoneinfo's problem, not ours. The zone
# and its label are fixed together: --digest-tz moves only the digest's
# clock, never the wording, so the label cannot drift from the zone.
DEFAULT_TZ = "America/Toronto"
TZ_LABEL = "ET"
LOCAL_TZ = ZoneInfo(DEFAULT_TZ)
AM, PM = "AM", "PM"
NOON = 12

# health.json's vocabulary (scripts/eval_dashboard/health.py owns it).
GREEN = "GREEN"
OUTAGE = "OUTAGE"
# The 24h window health.py reports metrics over, for a health.json that
# predates the `window_hours` field.
DEFAULT_WINDOW_HOURS = 24

# The kinds of message this script sends.
KIND_CHANGE = "change"  # a new state, condition or (in an OUTAGE) case list
KIND_RECOVERY = "recovery"  # back to GREEN, with how long it took
KIND_STALE = "stale"  # data.json stopped refreshing, or started again
KIND_DIGEST = "digest"  # the daily numbers
TOLD_KINDS = (KIND_CHANGE, KIND_RECOVERY, KIND_STALE)

# Where the message goes. The space is a resource name, the token a bearer
# credential minted by the workflow; the webhook is the legacy alternative.
SPACE_ENV = "CI_HEALTH_CHAT_SPACE"
TOKEN_ENV = "CI_HEALTH_CHAT_TOKEN"
WEBHOOK_ENV = "CI_HEALTH_CHAT_WEBHOOK"
CHAT_API_ROOT = "https://chat.googleapis.com/v1"
CHAT_MESSAGES_PATH = "{space}/messages"
CHAT_SCOPE = "https://www.googleapis.com/auth/chat.bot"
SPACE_PREFIX = "spaces/"
NOT_CONFIGURED = "webhook not configured: set CI_HEALTH_CHAT_SPACE (+ CI_HEALTH_CHAT_TOKEN) or CI_HEALTH_CHAT_WEBHOOK; nothing posted"
REQUEST_TIMEOUT_S = 30
USER_AGENT = "kube-agents-ci-health"

# The digest goes out once per local day (`--digest-tz`, Toronto by
# default), on the first tick inside [digest_hour:00 - window, digest_hour:00
# + window] local time. The job runs every 15 minutes, so a 20-minute window
# always contains at least one tick and the per-day marker in the state file
# (the local date) stops the second one from repeating it.
DEFAULT_DIGEST_HOUR = 9
DIGEST_WINDOW = timedelta(minutes=20)

# Inside an OUTAGE the cause grows as more cases collapse. Each growth is
# worth a message -- a reader deciding whether their red is the outage needs
# the current list -- but on 2026-09-02 the list changed nine times in ten
# hours, so a re-post needs a new case to have joined, and at most one per
# interval. A case dropping off is not news until the state changes.
OUTAGE_REPOST_INTERVAL = timedelta(hours=2)

DASHBOARD_URL = "https://storage.cloud.google.com/kube-agents-dashboards/evals/index.html"
# Every message ends with a deep link into the dashboard, on a line of its
# own so Chat auto-links it. The shape is a contract with the dashboard:
# `?cases=<comma-separated case ids>&since=<ISO 8601 UTC>[&until=<ISO 8601
# UTC>]` before the fragment, then `#gate` for an incident message and
# `#agent` for the digest. Commas and colons stay literal.
DASHBOARD_SECTION_GATE = "gate"
DASHBOARD_SECTION_AGENT = "agent"

# The message wording. One sentence of cause, one of what to do, then the
# link; the details live behind the link. Case names are read by a human
# deciding "is it me?": a family of cases is named by its family word, the
# prefix tokens below being too generic to be one.
GENERIC_TOKENS = frozenset({"cluster", "agent", "the", "a"})
CASES_NAMED_IN_FULL = 3
NO_ISSUE_TEXT = "no issue yet — file one with the presubmit-gate label"
# Mirrors health.py's STORM_COOLDOWN: a run started the minute the last
# storm-hit run finished still overlaps its tail.
STORM_COOLDOWN = timedelta(minutes=30)

# gsutil is how the state object is read and written; publish.py uses the
# same header so a reader never gets an hour-stale copy.
GSUTIL = "gsutil"
GS_PREFIX = "gs://"
CACHE_CONTROL = "Cache-Control: no-cache"

UTC = timezone.utc


def log(message: str) -> None:
    print(message, file=sys.stderr)


# --------------------------------------------------------------------------- #
# State file
# --------------------------------------------------------------------------- #


def read_state(location: str, runner=subprocess.run) -> dict | None:
    """The last posted state, or None when there is none yet."""
    if location.startswith(GS_PREFIX):
        result = runner([GSUTIL, "-q", "cat", location], capture_output=True, text=True)
        if result.returncode != 0:
            return None
        text = result.stdout
    else:
        path = pathlib.Path(location)
        if not path.is_file():
            return None
        text = path.read_text()
    try:
        loaded = json.loads(text)
    except ValueError:
        return None
    return loaded if isinstance(loaded, dict) else None


def write_state(location: str, state: dict, runner=subprocess.run) -> None:
    text = json.dumps(state, indent=2) + "\n"
    if location.startswith(GS_PREFIX):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write(text)
            name = handle.name
        try:
            runner([GSUTIL, "-q", "-h", CACHE_CONTROL, "cp", name, location], check=True)
        finally:
            os.unlink(name)
        return
    pathlib.Path(location).write_text(text)


# --------------------------------------------------------------------------- #
# Deciding what to say
# --------------------------------------------------------------------------- #


def parse_iso(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def local_date(now: datetime, tz=LOCAL_TZ) -> str:
    """The per-day digest marker: the date on the reader's clock."""
    return now.astimezone(tz).date().isoformat()


def in_digest_window(now: datetime, digest_hour: int, tz=LOCAL_TZ) -> bool:
    local = now.astimezone(tz)
    anchor = local.replace(hour=digest_hour, minute=0, second=0, microsecond=0)
    return anchor - DIGEST_WINDOW <= local <= anchor + DIGEST_WINDOW


def decide(health: dict, prev: dict | None, now: datetime, digest_hour: int, tz=LOCAL_TZ) -> list[str]:
    """Which message kinds go out this tick.

    A change is a new state, a new condition within the same state (a storm
    giving way to setup deaths is different advice), or -- inside an
    OUTAGE -- a new case joining, rate-limited. Staleness flipping either
    way is its own kind. The digest is independent of all of them.
    """
    kinds = []
    state = health.get("state")
    prev_state = (prev or {}).get("state")
    if prev is None:
        # First tick ever. A non-green start is worth a message; a green
        # one is not -- nobody needs to hear that nothing is wrong.
        if state != GREEN:
            kinds.append(KIND_CHANGE)
    elif state != prev_state:
        kinds.append(KIND_RECOVERY if state == GREEN else KIND_CHANGE)
    elif health.get("condition") != prev.get("condition"):
        kinds.append(KIND_CHANGE)
    elif state == OUTAGE:
        new_cases = set(health.get("failing_cases") or []) - set(prev.get("failing_cases") or [])
        last = parse_iso(prev.get("posted_at"))
        if new_cases and (last is None or now - last >= OUTAGE_REPOST_INTERVAL):
            kinds.append(KIND_CHANGE)

    if bool(health.get("stale")) != bool((prev or {}).get("stale")):
        kinds.append(KIND_STALE)

    if in_digest_window(now, digest_hour, tz) and (prev or {}).get("last_digest_date") != local_date(now, tz):
        kinds.append(KIND_DIGEST)
    return kinds


# --------------------------------------------------------------------------- #
# Rendering: plain Chat text, *bold*, bare URLs
# --------------------------------------------------------------------------- #


def duration_text(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes:02d}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def seconds_text(seconds) -> str:
    if seconds is None:
        return "n/a"
    return duration_text(timedelta(seconds=seconds))


def percent(value) -> str:
    return "n/a" if value is None else f"{round(value * 100)}%"


def clock(value: datetime | None, weekday: bool = False, label: bool = True) -> str:
    """A time on the reader's clock: "7:30 AM ET", "Sun 7:30 AM ET" with
    `weekday`, and without the zone label when it ends a range whose second
    half carries it ("1:15 PM–2:25 PM ET")."""
    if not value:
        return "?"
    local = value.astimezone(LOCAL_TZ)
    text = f"{local.hour % NOON or NOON}:{local.minute:02d} {AM if local.hour < NOON else PM}"
    if weekday:
        text = f"{local.strftime('%a')} {text}"
    return f"{text} {TZ_LABEL}" if label else text


def clock_range(start: datetime | None, end: datetime | None) -> str:
    return f"{clock(start, label=False)}–{clock(end)}"


def iso_z(value: datetime | None) -> str | None:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def dashboard_link(section: str, cases=(), since: datetime | None = None, until: datetime | None = None) -> str:
    """The deep link (see DASHBOARD_SECTION_*): query before fragment, empty
    parameters omitted, nothing percent-encoded -- case ids are slugs and
    the timestamps are the `Z` form."""
    params = []
    if cases:
        params.append("cases=" + ",".join(cases))
    if since:
        params.append(f"since={iso_z(since)}")
    if until:
        params.append(f"until={iso_z(until)}")
    query = "?" + "&".join(params) if params else ""
    return f"{DASHBOARD_URL}{query}#{section}"


def incident_link(health: dict, until: datetime | None = None) -> str:
    return dashboard_link(DASHBOARD_SECTION_GATE, health.get("failing_cases") or [], parse_iso(health.get("since")), until)


def describe_cases(cases) -> str:
    """The failing cases in the words a reader scans: one case by name, a
    family by its family word ("the 3 crashloop tests"), up to three by
    name, more as a count with the first three."""
    cases = list(cases)
    if not cases:
        return "tests"
    if len(cases) == 1:
        return cases[0]
    tokens = [case.split("-") for case in cases]
    shared = 0
    while all(len(t) > shared + 1 and t[shared] == tokens[0][shared] for t in tokens):
        shared += 1
    family = [t for t in tokens[0][:shared] if t not in GENERIC_TOKENS]
    if family:
        return f"the {len(cases)} {family[-1]} tests"
    if len(cases) <= CASES_NAMED_IN_FULL:
        return ", ".join(cases[:-1]) + " and " + cases[-1]
    return f"{len(cases)} tests ({', '.join(cases[:CASES_NAMED_IN_FULL])} and {len(cases) - CASES_NAMED_IN_FULL} more)"


def issue_tag(issue) -> str | None:
    """"#1278" for an {number, url} the bot filed or found; None otherwise."""
    number = (issue or {}).get("number") if isinstance(issue, dict) else None
    return f"#{number}" if number else None


def tracking_text(issues, issue=None) -> str:
    issues = list(issues or [])
    tag = issue_tag(issue)
    if tag and tag not in issues:
        issues.append(tag)
    return ", ".join(issues) if issues else NO_ISSUE_TEXT


def cause_sentence(health: dict) -> str:
    """One sentence a reader can answer "is it me?" from."""
    incident = health.get("incident") or {}
    prs = len(incident.get("prs") or [])
    since = clock(parse_iso(health.get("since")))
    condition = health.get("condition")
    if condition == "shared_break":
        return (
            f"{describe_cases(health.get('failing_cases'))} fail on every PR since {since}"
            f" ({prs} PRs so far). Shared test fixture, not your code."
        )
    if condition == "storm":
        start, end = parse_iso(incident.get("window_start")), parse_iso(incident.get("window_end"))
        window = clock_range(start, end) if start and end else f"since {since}"
        return f"quota storm {window} hit {prs} PRs."
    if condition == "setup_deaths":
        return f"{incident.get('runs', 0)} runs on {prs} PRs died during setup since {since}."
    return health.get("cause") or "no single cause"


def render_change(health: dict, prev: dict | None, issue: dict | None = None) -> str:
    condition = health.get("condition")
    if health.get("state") == OUTAGE:
        lines = [
            f"🔴 *Smoke gate: broken* — {cause_sentence(health)}",
            f"Don't retest yet. Tracking {tracking_text(health.get('tracking_issues'), issue)}.",
        ]
    elif condition == "storm":
        end = parse_iso((health.get("incident") or {}).get("window_end"))
        when = f"after {clock(end + STORM_COOLDOWN)}" if end else "once the storm has passed"
        lines = [f"🟡 *Smoke gate: flaky* — {cause_sentence(health)}  Passing runs still count; if yours went red, retest {when}."]
    else:
        lines = [f"🟡 *Smoke gate: flaky* — {cause_sentence(health)}  Passing runs still count; if yours died before any test ran, retest."]
    lines.append(incident_link(health))
    return "\n".join(lines)


def render_stale(health: dict) -> str:
    refreshed = clock(parse_iso(health.get("generated_at")))
    if health.get("stale"):
        return f"⚪ *Smoke gate: no fresh data since {refreshed}* — the health bot can't see recent runs. Someone check the refresh job."
    return f"⚪ *Smoke gate: fresh data again* — refreshed {refreshed}; the gate reads {health.get('state', '?')}."


def short_cause(prev: dict) -> str:
    condition = prev.get("condition")
    if condition == "shared_break":
        return f"{describe_cases(prev.get('failing_cases'))} were failing"
    if condition == "storm":
        return "quota storm"
    if condition == "setup_deaths":
        return "setup failures"
    return prev.get("cause") or "unknown cause"


def render_recovery(health: dict, prev: dict, now: datetime) -> str:
    since = parse_iso(prev.get("since"))
    lasted = duration_text(now - since) if since else "a while"
    parts = [short_cause(prev)]
    issues = list(prev.get("tracking_issues") or [])
    tag = issue_tag(prev.get("issue"))
    if tag and tag not in issues:
        issues.append(tag)
    if issues:
        parts.append(", ".join(issues))
    # No "retests running" clause: this job queues none.
    lines = [
        f"🟢 *Smoke gate: healthy again* — fixed after {lasted} ({', '.join(parts)}).",
        # The closed incident: the cases and start the space was told, and
        # now as its end.
        dashboard_link(DASHBOARD_SECTION_GATE, prev.get("failing_cases") or [], since, now),
    ]
    return "\n".join(lines)


def render_digest(health: dict, now: datetime) -> str:
    metrics = health.get("metrics") or {}
    p50 = metrics.get("wall_clock_p50_s")
    typical = f"{int(p50 // 60)} min" if p50 is not None else "n/a"
    headline = (
        f"📊 *Smoke gate, last {metrics.get('window_hours', DEFAULT_WINDOW_HOURS)}h:*"
        f" {metrics.get('full_runs', 0)} runs · {metrics.get('green_runs', 0)} green"
        f" · {metrics.get('pr_caused_reds', 0)} PR-caused red · {metrics.get('infra_reds', 0)} infra"
        f" · typical run {typical}"
    )
    lines = [headline]
    if health.get("stale"):
        # The window is measured from the data's horizon, so during a stall
        # these are the same numbers every morning; say so every morning.
        lines.append(f"⚪ No fresh data since {clock(parse_iso(health.get('generated_at')))} — these numbers stop there. Someone check the refresh job.")
    lines.append(dashboard_link(DASHBOARD_SECTION_AGENT, health.get("failing_cases") or [], parse_iso(health.get("since"))))
    return "\n".join(lines)


def render(kind: str, health: dict, prev: dict | None, now: datetime, issue: dict | None = None) -> str:
    if kind == KIND_RECOVERY:
        return render_recovery(health, prev or {}, now)
    if kind == KIND_DIGEST:
        return render_digest(health, now)
    if kind == KIND_STALE:
        return render_stale(health)
    return render_change(health, prev, issue)


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #


class Sender:
    """One configured destination. `describe()` never includes a secret."""

    def __init__(self, space: str = "", token: str = "", webhook: str = "", opener=urllib.request.urlopen):
        self.space = space.strip()
        if self.space and not self.space.startswith(SPACE_PREFIX):
            self.space = SPACE_PREFIX + self.space
        self.token = token.strip()
        self.webhook = webhook.strip()
        self.opener = opener

    @classmethod
    def from_env(cls, environ=os.environ, opener=urllib.request.urlopen) -> Sender:
        return cls(
            space=environ.get(SPACE_ENV, ""),
            token=environ.get(TOKEN_ENV, ""),
            webhook=environ.get(WEBHOOK_ENV, ""),
            opener=opener,
        )

    @property
    def configured(self) -> bool:
        return bool(self.space and self.token) or bool(self.webhook)

    def describe(self) -> str:
        if self.space and self.token:
            return "chat api"
        if self.space:
            return f"chat api (space set, {TOKEN_ENV} missing)"
        return "webhook" if self.webhook else "unconfigured"

    def request(self, text: str) -> urllib.request.Request:
        body = json.dumps({"text": text}).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=UTF-8", "User-Agent": USER_AGENT}
        if self.space and self.token:
            url = f"{CHAT_API_ROOT}/{CHAT_MESSAGES_PATH.format(space=self.space)}"
            headers["Authorization"] = f"Bearer {self.token}"
        else:
            url = self.webhook
        return urllib.request.Request(url, data=body, headers=headers, method="POST")

    def send(self, text: str) -> bool:
        """POST once. True on 2xx; a failure is logged without the URL."""
        try:
            with self.opener(self.request(text), timeout=REQUEST_TIMEOUT_S) as response:
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            log(f"post failed: HTTP {exc.code} from {self.describe()}")
            return False
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            log(f"post failed: {type(exc).__name__} ({type(reason).__name__}) from {self.describe()}")
            return False
        if not 200 <= status < 300:
            log(f"post failed: HTTP {status} from {self.describe()}")
            return False
        return True


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def run(
    health: dict,
    prev: dict | None,
    now: datetime,
    digest_hour: int,
    sender: Sender,
    dry_run: bool,
    tz=LOCAL_TZ,
    tracker: gate_issue.Tracker | None = None,
) -> tuple[dict, list[tuple[str, str]], list[str]]:
    """Decide, render, send. Returns (new state, [(kind, text)], kinds that failed).

    `tracker`, when given, files the tracking issue a new OUTAGE lacks
    before the "broken" message is rendered (so it can say "Tracking #NNN")
    and comments on it after a recovery goes out."""
    kinds = decide(health, prev, now, digest_hour, tz)
    before = prev or {}
    issue = health.get("issue") or before.get("issue")
    wants_issue = KIND_CHANGE in kinds and health.get("state") == OUTAGE and not issue and not health.get("tracking_issues")
    if tracker is not None and wants_issue:
        since = parse_iso(health.get("since"))
        issue = tracker.ensure(health, now, clock(since, weekday=True), incident_link(health))
    messages = [(kind, render(kind, health, prev, now, issue)) for kind in kinds]
    failed = []
    for kind, text in messages:
        if dry_run:
            log(f"--dry-run: would post [{kind}]\n{text}\n")
        elif not sender.send(text):
            failed.append(kind)
    sent = [kind for kind in kinds if kind not in failed]
    if tracker is not None and KIND_RECOVERY in sent and before.get("issue"):
        began = parse_iso(before.get("since"))
        tracker.recovered(before["issue"], duration_text(now - began) if began else "a while")

    # The state file records what the space was last TOLD, kind by kind,
    # so the next tick asks its questions -- did the state change, did a new
    # case join, did staleness flip -- against what the readers have. A sent
    # change or recovery advances the state, condition, cause and case list;
    # a sent stale notice advances the stale bit; a sent digest advances the
    # digest date; nothing else moves. A kind that failed, or was not due,
    # leaves its part where it was, so the next tick re-asks exactly that
    # question: a change that failed beside a stale notice that succeeded is
    # posted next tick, and a stale flip posted mid-OUTAGE does not swallow a
    # case that joined inside OUTAGE_REPOST_INTERVAL. The tracking issue
    # rides along while the recorded state is not GREEN and is dropped by
    # the recovery; health.py reads it back from here (`--posted-state`)
    # into the next health.json.
    told_state = KIND_CHANGE in sent or KIND_RECOVERY in sent
    told_stale = KIND_STALE in sent
    if prev is None:
        # First tick: whatever was not due is recorded as told, so a green,
        # fresh start is not announced later as a change.
        told_state = told_state or (KIND_CHANGE not in kinds and KIND_RECOVERY not in kinds)
        told_stale = told_stale or KIND_STALE not in kinds
    source = health if told_state else before
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "state": source.get("state"),
        "condition": source.get("condition"),
        "cause": source.get("cause"),
        "failing_cases": source.get("failing_cases") or [],
        "tracking_issues": source.get("tracking_issues") or [],
        "since": source.get("since"),
        "issue": issue if source.get("state") not in (None, GREEN) else None,
        "stale": bool(health.get("stale")) if told_stale else bool(before.get("stale")),
        "posted_at": before.get("posted_at"),
        "last_digest_date": before.get("last_digest_date"),
        "updated_at": now.isoformat(timespec="seconds"),
    }
    if KIND_CHANGE in sent or KIND_RECOVERY in sent:
        state["posted_at"] = now.isoformat(timespec="seconds")
    if KIND_DIGEST in sent:
        state["last_digest_date"] = local_date(now, tz)
    return state, messages, failed


def parse_tz(name: str):
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"unknown time zone {name!r}") from exc


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--health", type=pathlib.Path, required=True, help="the health.json health.py wrote")
    parser.add_argument("--state", required=True, help="last-posted state: local path or gs:// object (this script's only write)")
    parser.add_argument("--digest-hour", type=int, default=DEFAULT_DIGEST_HOUR, help="hour of the daily digest, in --digest-tz")
    parser.add_argument("--digest-tz", type=parse_tz, default=LOCAL_TZ, help=f"IANA zone the digest hour and day are read in (default {DEFAULT_TZ}); times in messages stay {DEFAULT_TZ} ({TZ_LABEL}) regardless")
    parser.add_argument("--repo", default=ghcli.DEFAULT_REPO, help="owner/repo the tracking issue is filed in")
    parser.add_argument("--now", help="evaluate as of this ISO 8601 time (default: now)")
    parser.add_argument("--dry-run", action="store_true", help="print the messages (and the issue) instead of posting; still updates --state")
    return parser.parse_args(argv)


def main(argv=None, environ=os.environ, opener=urllib.request.urlopen, runner=subprocess.run, gh_runner=None) -> int:
    args = parse_args(argv)
    try:
        health = json.loads(args.health.read_text())
    except (OSError, ValueError) as exc:
        log(f"ERROR: {args.health}: {exc}")
        return 1
    now = parse_iso(args.now) or datetime.now(UTC)
    sender = Sender.from_env(environ, opener)
    if not sender.configured and not args.dry_run:
        log(NOT_CONFIGURED)
        return 0
    # The issue is filed with the workflow's token; without one (a laptop
    # dry run, a muted job) nothing is filed and the message says so.
    tracker = None
    if environ.get(ghcli.TOKEN_ENV) or args.dry_run:
        tracker = gate_issue.Tracker(ghcli.Gh(args.repo, gh_runner or runner, dry_run=args.dry_run))

    prev = read_state(args.state, runner)
    state, messages, failed = run(health, prev, now, args.digest_hour, sender, args.dry_run, args.digest_tz, tracker)
    if prev is None and failed and len(failed) == len(messages):
        # Nothing has ever been told and nothing got through: there is no
        # state worth recording, and the next tick starts from scratch.
        log("not recording state: every post failed on the first tick")
        return 1
    # Always written past this point, even after a partial failure: the
    # parts that were told are recorded and the failed kind is re-asked next
    # tick from the same file.
    write_state(args.state, state, runner)
    sent = ", ".join(kind for kind, _ in messages if kind not in failed) or "nothing"
    log(f"{health.get('state')} via {sender.describe()}: posted {sent}")
    if failed:
        log(f"failed to post: {', '.join(failed)}; the next tick retries")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
