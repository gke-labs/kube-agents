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

STATE_SCHEMA_VERSION = 1

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

# The digest goes out once per UTC day, on the first tick inside
# [digest_hour:00 - window, digest_hour:00 + window]. The job runs every 15
# minutes, so a 20-minute window always contains at least one tick and the
# per-day marker in the state file stops the second one from repeating it.
DEFAULT_DIGEST_HOUR = 8
DIGEST_WINDOW = timedelta(minutes=20)

# Inside an OUTAGE the cause grows as more cases collapse. Each growth is
# worth a message -- a reader deciding whether their red is the outage needs
# the current list -- but on 2026-09-02 the list changed nine times in ten
# hours, so a re-post needs a new case to have joined, and at most one per
# interval. A case dropping off is not news until the state changes.
OUTAGE_REPOST_INTERVAL = timedelta(hours=2)

DASHBOARD_URL = "https://storage.cloud.google.com/kube-agents-dashboards/evals/index.html"

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


def in_digest_window(now: datetime, digest_hour: int) -> bool:
    anchor = now.replace(hour=digest_hour, minute=0, second=0, microsecond=0)
    return anchor - DIGEST_WINDOW <= now <= anchor + DIGEST_WINDOW


def decide(health: dict, prev: dict | None, now: datetime, digest_hour: int) -> list[str]:
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

    today = now.date().isoformat()
    if in_digest_window(now, digest_hour) and (prev or {}).get("last_digest_date") != today:
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


def hhmm(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%H:%M") if value else "?"


def render_change(health: dict, prev: dict | None) -> str:
    was = f" (was {prev['state']})" if prev and prev.get("state") and prev["state"] != health["state"] else ""
    lines = [f"*CI health: {health['state']}*{was} — {health.get('cause') or 'no single cause'}"]
    for line in health.get("evidence") or []:
        lines.append(f"• {line}")
    if health.get("advice"):
        lines.append(f"Advice: {health['advice']}")
    lines.append(f"Dashboard: {DASHBOARD_URL}")
    return "\n".join(lines)


def render_stale(health: dict) -> str:
    metrics = health.get("metrics") or {}
    age = metrics.get("data_age_s")
    if health.get("stale"):
        head = (
            f"*CI health: data is stale* — data.json last refreshed {health.get('generated_at', '?')}"
            f" ({seconds_text(age)} ago); the dashboard refresh job has stalled,"
            f" so the {health.get('state', '?')} above it is that old."
        )
    else:
        head = f"*CI health: data is fresh again* — data.json refreshed {health.get('generated_at', '?')}; state {health.get('state', '?')}."
    return "\n".join([head, f"Dashboard: {DASHBOARD_URL}"])


def render_recovery(health: dict, prev: dict, now: datetime) -> str:
    since = parse_iso(prev.get("since"))
    lasted = f" after {duration_text(now - since)}" if since else ""
    cause = f" ({prev['cause']})" if prev.get("cause") else ""
    lines = [
        f"*CI health: GREEN* — recovered{lasted} of {prev.get('state', 'trouble')}{cause}",
        f"Dashboard: {DASHBOARD_URL}",
    ]
    return "\n".join(lines)


def render_digest(health: dict, now: datetime) -> str:
    metrics = health.get("metrics") or {}
    since = parse_iso(health.get("since"))
    headline = f"*CI health daily digest ({now.date().isoformat()})* — {health.get('state', '?')} since {hhmm(since)} UTC"
    if health.get("cause"):
        headline += f": {health['cause']}"
    lines = [headline]
    if health.get("stale"):
        lines.append(f"Data is stale: data.json last refreshed {health.get('generated_at', '?')} ({seconds_text(metrics.get('data_age_s'))} ago).")
    hours = metrics.get("window_hours", DEFAULT_WINDOW_HOURS)
    lines.append(
        f"Last {hours}h: {metrics.get('full_runs', 0)} full runs on {metrics.get('prs', 0)} PRs,"
        f" {metrics.get('green_runs', 0)} green ({percent(metrics.get('green_rate'))}),"
        f" wall clock p50 {seconds_text(metrics.get('wall_clock_p50_s'))}"
        f" / p90 {seconds_text(metrics.get('wall_clock_p90_s'))},"
        f" infra reps {percent(metrics.get('infra_rep_rate'))},"
        f" setup failures {metrics.get('setup_deaths', 0)},"
        f" aborted {metrics.get('aborted_runs', 0)}"
    )
    fixtures = metrics.get("fixtures")
    if isinstance(fixtures, dict):
        healed = fixtures.get("healed")
        broken = fixtures.get("broken")
        projects = fixtures.get("projects")
        parts = []
        if healed is not None:
            parts.append(f"{healed} healed")
        if broken is not None:
            parts.append(f"{broken} broken")
        if projects is not None:
            parts.append(f"across {projects} projects")
        if parts:
            lines.append("Fixtures: " + ", ".join(parts))
    if health.get("advice"):
        lines.append(f"Advice: {health['advice']}")
    lines.append(f"Dashboard: {DASHBOARD_URL}")
    return "\n".join(lines)


def render(kind: str, health: dict, prev: dict | None, now: datetime) -> str:
    if kind == KIND_RECOVERY:
        return render_recovery(health, prev or {}, now)
    if kind == KIND_DIGEST:
        return render_digest(health, now)
    if kind == KIND_STALE:
        return render_stale(health)
    return render_change(health, prev)


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


def run(health: dict, prev: dict | None, now: datetime, digest_hour: int, sender: Sender, dry_run: bool) -> tuple[dict, list[tuple[str, str]], list[str]]:
    """Decide, render, send. Returns (new state, [(kind, text)], kinds that failed)."""
    kinds = decide(health, prev, now, digest_hour)
    messages = [(kind, render(kind, health, prev, now)) for kind in kinds]
    failed = []
    for kind, text in messages:
        if dry_run:
            log(f"--dry-run: would post [{kind}]\n{text}\n")
        elif not sender.send(text):
            failed.append(kind)
    sent = [kind for kind in kinds if kind not in failed]

    # The state file records what the space was last TOLD, not what is
    # currently true, so the next tick asks its questions -- did the state
    # change, did a new case join, did staleness flip -- against what the
    # readers have. A change that failed to post therefore leaves the told
    # state where it was and the next tick posts it again; a digest that
    # failed beside a change that succeeded does not make the change repeat.
    told_now = any(kind in TOLD_KINDS for kind in sent)
    due_but_failed = any(kind in TOLD_KINDS for kind in failed)
    told = health if told_now or (prev is None and not due_but_failed) else (prev or {})
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "state": told.get("state"),
        "condition": told.get("condition"),
        "cause": told.get("cause"),
        "failing_cases": told.get("failing_cases") or [],
        "stale": bool(told.get("stale")),
        "since": told.get("since"),
        "posted_at": (prev or {}).get("posted_at"),
        "last_digest_date": (prev or {}).get("last_digest_date"),
        "updated_at": now.isoformat(timespec="seconds"),
    }
    if told_now:
        state["posted_at"] = now.isoformat(timespec="seconds")
    if KIND_DIGEST in sent:
        state["last_digest_date"] = now.date().isoformat()
    return state, messages, failed


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--health", type=pathlib.Path, required=True, help="the health.json health.py wrote")
    parser.add_argument("--state", required=True, help="last-posted state: local path or gs:// object (this script's only write)")
    parser.add_argument("--digest-hour", type=int, default=DEFAULT_DIGEST_HOUR, help="UTC hour of the daily digest")
    parser.add_argument("--now", help="evaluate as of this ISO 8601 time (default: now)")
    parser.add_argument("--dry-run", action="store_true", help="print the messages instead of posting; still updates --state")
    return parser.parse_args(argv)


def main(argv=None, environ=os.environ, opener=urllib.request.urlopen, runner=subprocess.run) -> int:
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

    prev = read_state(args.state, runner)
    state, messages, failed = run(health, prev, now, args.digest_hour, sender, args.dry_run)
    if prev is None and failed and len(failed) == len(messages):
        # Nothing has ever been told and nothing got through: there is no
        # state worth recording, and the next tick starts from scratch.
        log("not recording state: every post failed on the first tick")
        return 1
    write_state(args.state, state, runner)
    sent = ", ".join(kind for kind, _ in messages if kind not in failed) or "nothing"
    log(f"{health.get('state')} via {sender.describe()}: posted {sent}")
    if failed:
        log(f"failed to post: {', '.join(failed)}; the next tick retries")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
